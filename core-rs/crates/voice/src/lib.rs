//! DEMASCAS's voice (replaces `daemon/src/tts.cpp`).
//!
//! Sentences are queued; a worker thread synthesizes each with Kokoro and
//! hands the audio to the speaker, so sentence N+1 is being made while N
//! plays.  Fixed phrases ("Standing by.") are synthesized once, ahead of
//! time, and play instantly.  `interrupt()` silences at once (barge-in);
//! `hold(true)` keeps new sentences from starting while the user talks.
//! Without Kokoro, the system voice (`say` / `espeak-ng`) is used.

use std::collections::HashMap;
use std::path::Path;
use std::process::{Child, Command};
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use std::time::Duration;

use audio::Speaker;

/// Speakable text: no markdown, URLs, JSON or stray symbols; ≤ 500 chars.
pub fn sanitize(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let c: Vec<char> = text.chars().collect();
    let mut i = 0;
    while i < c.len() {
        match c[i] {
            '*' | '"' | '\\' | '$' | '#' | '_' if c[i] != '_' || (i > 0 && c[i - 1] == ' ') || i == 0 => i += 1,
            '`' => {
                i += 1;
                while i < c.len() && c[i] != '`' {
                    i += 1;
                }
                i += 1;
            }
            '[' => {
                // [label](url) → label
                if let Some(j) = c[i..].iter().position(|&x| x == ']').map(|p| p + i) {
                    if c.get(j + 1) == Some(&'(') {
                        out.extend(&c[i + 1..j]);
                        i = c[j..].iter().position(|&x| x == ')').map(|p| p + j + 1).unwrap_or(c.len());
                        continue;
                    }
                }
                out.push('[');
                i += 1;
            }
            '{' => {
                let mut depth = 1;
                i += 1;
                while i < c.len() && depth > 0 {
                    if c[i] == '{' {
                        depth += 1;
                    } else if c[i] == '}' {
                        depth -= 1;
                    }
                    i += 1;
                }
            }
            'h' if c[i..].starts_with(&['h', 't', 't', 'p']) && (c[i..].starts_with(&['h', 't', 't', 'p', ':']) || c[i..].starts_with(&['h', 't', 't', 'p', 's', ':'])) => {
                while i < c.len() && !c[i].is_whitespace() {
                    i += 1;
                }
            }
            ch => {
                out.push(ch);
                i += 1;
            }
        }
    }
    let out: String = out.split_whitespace().collect::<Vec<_>>().join(" ");
    out.chars().take(500).collect()
}

/// Split into sentences at . ! ? (keeps very short fragments together).
pub fn sentences(text: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut cur = String::new();
    let chars: Vec<char> = text.chars().collect();
    for (i, &ch) in chars.iter().enumerate() {
        cur.push(ch);
        let end = matches!(ch, '.' | '!' | '?') && chars.get(i + 1).is_none_or(|n| n.is_whitespace());
        if end && cur.trim().len() >= 12 {
            out.push(cur.trim().to_string());
            cur.clear();
        }
    }
    if !cur.trim().is_empty() {
        out.push(cur.trim().to_string());
    }
    out
}

/// Phrases synthesized ahead of time (instant).
pub const CACHED: &[&str] = &[
    "At your service.", "Standing by.", "Done.", "Okay.", "Okay, cancelled.", "I'm here. What can I do for you?",
    "Okay, I've stopped.", "Okay, stopping. Standing by.", "Sorry, I didn't catch that. Could you say it again?",
    "Sorry — yes or no?", "Okay, I won't do that.",
];

enum Engine {
    Kokoro(sherpa::Tts),
    System,
}

struct State {
    gen: AtomicU64,
    /// Sentences queued or being made.
    pending: AtomicUsize,
    held: AtomicBool,
    child: Mutex<Option<Child>>,
}

pub struct Voice {
    tx: mpsc::Sender<(u64, String)>,
    st: Arc<State>,
    speaker: Arc<Speaker>,
    pub backend: &'static str,
}

impl Voice {
    /// Kokoro from `kokoro_dir` if it loads, else the system voice.
    pub fn new(speaker: Arc<Speaker>, kokoro_dir: &Path, threads: i32) -> Self {
        let engine = match sherpa::Tts::kokoro(kokoro_dir, threads, 1.0) {
            Ok(t) => Engine::Kokoro(t),
            Err(e) => {
                eprintln!("[!] Kokoro unavailable ({e}) — using the system voice");
                Engine::System
            }
        };
        let backend = if matches!(engine, Engine::Kokoro(_)) { "Kokoro" } else { "system voice" };
        let st = Arc::new(State { gen: AtomicU64::new(0), pending: AtomicUsize::new(0), held: AtomicBool::new(false), child: Mutex::new(None) });
        let (tx, rx) = mpsc::channel::<(u64, String)>();
        let (st2, sp2) = (st.clone(), speaker.clone());
        thread::Builder::new().name("voice".into()).spawn(move || worker(engine, rx, st2, sp2)).expect("voice thread");
        Self { tx, st, speaker, backend }
    }

    /// Queue text to speak (returns at once).
    pub fn say(&self, text: &str) {
        let clean = sanitize(text);
        if clean.chars().all(|c| !c.is_alphanumeric()) {
            return;
        }
        let gen = self.st.gen.load(Ordering::SeqCst);
        for s in sentences(&clean) {
            self.st.pending.fetch_add(1, Ordering::SeqCst);
            let _ = self.tx.send((gen, s));
        }
    }

    /// Stop now: queued sentences are dropped, playback stops.
    pub fn interrupt(&self) {
        self.st.gen.fetch_add(1, Ordering::SeqCst);
        self.speaker.stop();
        if let Some(mut c) = self.st.child.lock().unwrap().take() {
            let _ = c.kill();
        }
    }

    /// While held, no NEW sentence starts (the user is talking).
    pub fn hold(&self, on: bool) {
        self.st.held.store(on, Ordering::SeqCst);
    }

    pub fn is_speaking(&self) -> bool {
        self.st.pending.load(Ordering::SeqCst) > 0 || self.speaker.is_playing() || self.st.child.lock().unwrap().is_some()
    }

    pub fn wait_done(&self, max: Duration) {
        let t = std::time::Instant::now();
        while self.is_speaking() && t.elapsed() < max {
            thread::sleep(Duration::from_millis(20));
        }
    }
}

fn worker(engine: Engine, rx: mpsc::Receiver<(u64, String)>, st: Arc<State>, speaker: Arc<Speaker>) {
    let mut cache: HashMap<String, Vec<f32>> = HashMap::new();
    let rate = match &engine {
        Engine::Kokoro(t) => t.sample_rate,
        Engine::System => 0,
    };
    let mut warmed = false;
    loop {
        let msg = match rx.recv_timeout(Duration::from_millis(300)) {
            Ok(m) => m,
            Err(mpsc::RecvTimeoutError::Timeout) => {
                // idle: synthesize the fixed phrases once
                if !warmed {
                    if let Engine::Kokoro(t) = &engine {
                        for p in CACHED {
                            cache.insert(p.to_string(), t.generate(p, 0, 1.0));
                        }
                    }
                    warmed = true;
                }
                continue;
            }
            Err(_) => return,
        };
        let (gen, text) = msg;
        while st.held.load(Ordering::SeqCst) && gen == st.gen.load(Ordering::SeqCst) {
            thread::sleep(Duration::from_millis(30));
        }
        if gen == st.gen.load(Ordering::SeqCst) {
            match &engine {
                Engine::Kokoro(t) => {
                    let pcm = match cache.get(&text) {
                        Some(p) => p.clone(),
                        None => t.generate(&text, 0, 1.0),
                    };
                    if gen == st.gen.load(Ordering::SeqCst) && !pcm.is_empty() {
                        // short lead-in so the first word isn't clipped
                        let mut v = vec![0.0f32; rate as usize / 50];
                        v.extend(pcm);
                        speaker.play(&v, rate);
                    }
                }
                Engine::System => {
                    let cmd = if cfg!(target_os = "macos") { Command::new("say").arg(&text).spawn() } else { Command::new("espeak-ng").arg(&text).spawn() };
                    if let Ok(child) = cmd {
                        *st.child.lock().unwrap() = Some(child);
                        loop {
                            let done = st.child.lock().unwrap().as_mut().map(|c| c.try_wait().ok().flatten().is_some()).unwrap_or(true);
                            if done {
                                break;
                            }
                            thread::sleep(Duration::from_millis(30));
                        }
                        st.child.lock().unwrap().take();
                    }
                }
            }
        }
        st.pending.fetch_sub(1, Ordering::SeqCst);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn speakable_text() {
        assert_eq!(sanitize("**Done!** See [the docs](https://x.io) or https://y.com now."), "Done! See the docs or now.");
        assert_eq!(sanitize(r#"Opened {"tool":"x"} it"#), "Opened it");
        assert_eq!(sanitize("snake_case stays"), "snake_case stays");
    }

    #[test]
    fn sentence_split() {
        assert_eq!(sentences("Paris is the capital. It is lovely!"), vec!["Paris is the capital.", "It is lovely!"]);
        assert_eq!(sentences("Hi. Okay then, bye."), vec!["Hi. Okay then, bye."]);
        assert_eq!(sentences("Version 2.5 is out."), vec!["Version 2.5 is out."]);
    }
}
