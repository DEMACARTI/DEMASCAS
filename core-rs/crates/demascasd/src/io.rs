//! The daemon's ears and mouth.  The state machine only sees these two
//! traits, so text mode (the exam) and voice mode (mic + Kokoro) run the
//! very same conversation logic.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::io::{AsyncBufReadExt, BufReader, Lines, Stdin};

/// Something the ears produced.
#[derive(Debug, Clone, PartialEq)]
pub enum Heard {
    /// Speech, with the recogniser's confidence (0–1) and unsure share.
    Speech { text: String, confidence: f32, unsure: f32 },
    /// The wake word (only reported while sleeping).
    Wake,
    /// Nothing within the timeout.
    Silence,
    /// Exam only: forget everything, sleep.
    Reset,
    /// Input is gone — shut down.
    Closed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    Sleeping,
    Listening,
}

pub trait Ears: Send {
    /// The next thing heard.  `timeout` = None waits as long as it takes.
    fn next(&mut self, mode: Mode, timeout: Option<Duration>)
        -> impl std::future::Future<Output = Heard> + Send;
    /// Something already heard, without waiting (text mode: a line typed
    /// while a task runs).  Voice mode hears through `next`.
    fn try_now(&mut self) -> Option<Heard> {
        None
    }
    /// The last thing heard began as a barge-in (talking over DEMASCAS) —
    /// it may be DEMASCAS's own voice coming back.
    fn barged(&self) -> bool {
        false
    }
}

pub trait Mouth: Send + Sync {
    fn say(&self, text: &str);
    /// Stop speaking now (barge-in, cancel).
    fn interrupt(&self);
    fn is_speaking(&self) -> bool;
    /// Wait until everything queued has been spoken.
    fn wait_done(&self) -> impl std::future::Future<Output = ()> + Send;
    /// A short earcon ("Glass" on wake); no-op where there's no audio.
    fn chime(&self, _name: &str) {}
}

// ── text mode (DEMASCAS_TEXT_MODE=1) — same protocol as the C++ daemon ──
//   in   /wake · /reset · ~0.4|text · text
//   out  @@READY <state> · @@SAY <text> · @@WAKE · @@RESET

fn emit(tag: &str, text: &str) {
    use std::io::Write;
    let mut line = format!("\n@@{tag}");
    if !text.is_empty() {
        line.push(' ');
        line.push_str(&text.replace(['\n', '\r'], " "));
    }
    line.push('\n');
    let mut out = std::io::stdout().lock();
    let _ = out.write_all(line.as_bytes());
    let _ = out.flush();
}

pub fn emit_event(tag: &str) {
    emit(tag, "");
}

pub struct TextEars {
    rx: tokio::sync::mpsc::UnboundedReceiver<String>,
    closed: bool,
    /// A control line (/reset, /wake) seen by try_now — kept for next().
    stash: Option<String>,
}

impl TextEars {
    pub fn new() -> Self {
        // read stdin in the background so a line typed WHILE a task runs
        // can be picked up (try_now) — "what are you doing?", "stop"
        let (tx, rx) = tokio::sync::mpsc::unbounded_channel();
        tokio::spawn(async move {
            let mut lines: Lines<BufReader<Stdin>> = BufReader::new(tokio::io::stdin()).lines();
            while let Ok(Some(l)) = lines.next_line().await {
                if tx.send(l).is_err() {
                    break;
                }
            }
        });
        Self { rx, closed: false, stash: None }
    }

    fn parse(line: String, mode: Mode) -> Option<Heard> {
        let line = line.trim_end().to_string();
        match line.as_str() {
            "/reset" => return Some(Heard::Reset),
            "/wake" => return Some(Heard::Wake),
            _ if mode == Mode::Sleeping => return None, // asleep: only the wake word counts
            _ => {}
        }
        let (confidence, text) = match line.strip_prefix('~').and_then(|r| r.split_once('|')) {
            Some((c, t)) => (c.parse().unwrap_or(1.0), t.to_string()),
            None => (1.0, line),
        };
        if text.is_empty() {
            return Some(Heard::Silence);
        }
        Some(Heard::Speech { text, confidence, unsure: if confidence < 0.5 { 0.4 } else { 0.0 } })
    }
}

impl Default for TextEars {
    fn default() -> Self {
        Self::new()
    }
}

impl Ears for TextEars {
    async fn next(&mut self, mode: Mode, _timeout: Option<Duration>) -> Heard {
        if self.closed {
            return Heard::Closed;
        }
        emit("READY", if mode == Mode::Sleeping { "SLEEPING" } else { "LISTENING" });
        loop {
            let line = match self.stash.take() {
                Some(l) => l,
                None => match self.rx.recv().await {
                    Some(l) => l,
                    None => {
                        self.closed = true;
                        return Heard::Closed;
                    }
                },
            };
            match Self::parse(line, mode) {
                Some(h) => return h,
                None => emit("READY", "SLEEPING"),
            }
        }
    }

    fn try_now(&mut self) -> Option<Heard> {
        if self.stash.is_some() {
            return None;
        }
        let line = self.rx.try_recv().ok()?;
        match Self::parse(line.clone(), Mode::Listening) {
            Some(h @ Heard::Speech { .. }) => Some(h),
            _ => {
                self.stash = Some(line); // not speech — next() will handle it
                None
            }
        }
    }
}

#[derive(Default)]
pub struct TextMouth {
    pub said: Arc<Mutex<Vec<String>>>,
    quiet: AtomicBool,
}

impl Mouth for TextMouth {
    fn say(&self, text: &str) {
        if text.trim().is_empty() || self.quiet.load(Ordering::Relaxed) {
            return;
        }
        emit("SAY", text);
        self.said.lock().unwrap().push(text.to_string());
    }
    fn interrupt(&self) {}
    fn is_speaking(&self) -> bool {
        false
    }
    async fn wait_done(&self) {}
}
