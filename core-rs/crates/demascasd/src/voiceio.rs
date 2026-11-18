//! Voice mode: the microphone + recogniser as [`Ears`], Kokoro as [`Mouth`].
//! Ported from the SLEEPING / LISTENING / barge-in code in `daemon/src/main.cpp`.

use std::sync::Arc;
use std::time::{Duration, Instant};

use audio::{rms, BargeIn, EchoCanceller, Mic, RATE};
use brain::utterance::is_wake_phrase;
use speech::Recognizer;
use voice::Voice;

use crate::io::{Ears, Heard, Mode, Mouth};

const WAKE_WINDOW_SEC: f32 = 2.0;
const WAKE_HOP: Duration = Duration::from_millis(750);
/// Loudness gate for running the wake model at all (most of the time: quiet).
const WAKE_ENERGY: f32 = 0.008 * 0.3;
const POLL: Duration = Duration::from_millis(50);
const SILENCE_END: Duration = Duration::from_millis(1000);
const POST_SPEECH_GUARD: Duration = Duration::from_millis(250);
const TTS_COOLDOWN: Duration = Duration::from_millis(1000);

pub struct VoiceMouth {
    pub voice: Voice,
}

impl Mouth for VoiceMouth {
    fn say(&self, text: &str) {
        println!("[🗣 DEMASCAS]: {text}");
        self.voice.say(text);
    }
    fn interrupt(&self) {
        self.voice.interrupt();
    }
    fn is_speaking(&self) -> bool {
        self.voice.is_speaking()
    }
    async fn wait_done(&self) {
        let t = Instant::now();
        while self.voice.is_speaking() && t.elapsed() < Duration::from_secs(60) {
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    }
    fn chime(&self, name: &str) {
        #[cfg(target_os = "macos")]
        {
            let _ = std::process::Command::new("afplay").arg(format!("/System/Library/Sounds/{name}.aiff")).spawn();
        }
        #[cfg(not(target_os = "macos"))]
        let _ = name;
    }
}

pub struct VoiceEars {
    pub mic: Mic,
    pub aec: Arc<EchoCanceller>,
    pub vad: sherpa::Vad,
    pub barge_vad: sherpa::Vad,
    pub recog: Recognizer,
    pub mouth: Arc<VoiceMouth>,
    pub barge: BargeIn,
    wake_window: Vec<f32>,
    prev_hop_speech: bool,
    last_spoke: Instant,
    empty_hops: u32,
    barged: bool,
}

impl VoiceEars {
    pub fn new(mic: Mic, aec: Arc<EchoCanceller>, vad: sherpa::Vad, barge_vad: sherpa::Vad, recog: Recognizer, mouth: Arc<VoiceMouth>) -> Self {
        Self { mic, aec, vad, barge_vad, recog, mouth, barge: BargeIn::default(), wake_window: Vec::new(), prev_hop_speech: false, last_spoke: Instant::now() - Duration::from_secs(5), empty_hops: 0, barged: false }
    }

    async fn wait_for_wake(&mut self) -> Heard {
        loop {
            if self.mouth.is_speaking() {
                tokio::time::sleep(Duration::from_millis(100)).await;
                self.mic.drain();
                self.wake_window.clear();
                self.last_spoke = Instant::now();
                continue;
            }
            if self.last_spoke.elapsed() < TTS_COOLDOWN {
                tokio::time::sleep(Duration::from_millis(100)).await;
                self.mic.drain();
                self.wake_window.clear();
                continue;
            }
            tokio::time::sleep(WAKE_HOP).await;
            let hop = self.mic.drain();
            if hop.is_empty() {
                self.empty_hops += 1;
                if self.empty_hops == 4 {
                    eprintln!("[!] The microphone is delivering no audio. Grant Microphone access to the app running DEMASCAS (System Settings → Privacy & Security → Microphone), then restart.");
                }
                continue;
            }
            let hop_speech = rms(&hop) >= WAKE_ENERGY;
            self.wake_window.extend_from_slice(&hop);
            let keep = (WAKE_WINDOW_SEC * RATE as f32) as usize;
            if self.wake_window.len() > keep {
                let n = self.wake_window.len() - keep;
                self.wake_window.drain(..n);
            }
            // while there's sound, plus one hop after (a wake word at the end)
            let check = hop_speech || self.prev_hop_speech;
            self.prev_hop_speech = hop_speech;
            if !check {
                continue;
            }
            let window = self.wake_window.clone();
            let heard = tokio::task::block_in_place(|| self.recog.wake(&window));
            if !heard.is_empty() && !heard.to_lowercase().contains("blank") {
                println!("[👂 heard] {heard:?}");
            }
            if is_wake_phrase(&heard) {
                self.wake_window.clear();
                self.prev_hop_speech = false;
                return Heard::Wake;
            }
        }
    }

    /// Watch for the user talking over DEMASCAS.  Returns what they've said so far.
    async fn barge_in_monitor(&mut self) -> Option<Vec<f32>> {
        self.barge.reset();
        self.barge_vad.reset();
        let mut recent: Vec<f32> = Vec::new();
        let keep = (RATE as f32 * 0.8) as usize;
        while self.mouth.is_speaking() {
            tokio::time::sleep(POLL).await;
            let (raw, t_end) = self.mic.drain_timed();
            if raw.is_empty() {
                continue;
            }
            let clean = self.aec.process(&raw, t_end);
            if clean.is_empty() {
                continue;
            }
            self.barge_vad.accept(&clean);
            recent.extend_from_slice(&clean);
            if recent.len() > keep {
                let n = recent.len() - keep;
                recent.drain(..n);
            }
            let (mic_rms, clean_rms) = (rms(&raw), rms(&clean));
            let speech = self.barge_vad.speaking();
            // the canceller latches "converged" — but if it's only removing a
            // few dB right now (live log: 2–4 dB), our own voice survives it
            // and looks like the user; then only a voice louder than the
            // speakers counts
            let trusted = self.aec.converged() && self.aec.echo_reduction_db() >= 8.0;
            if self.barge.update(mic_rms, clean_rms, speech, trusted, speech) {
                let how = if self.aec.converged() { format!("echo reduction {:.0} dB", self.aec.echo_reduction_db()) } else { "echo canceller still learning".into() };
                println!("[!] BARGE-IN (voice {clean_rms:.3} after echo removal, {how})");
                self.mouth.interrupt();
                return Some(recent);
            }
        }
        None
    }

    /// Wait for speech, record until silence.  None = DEMASCAS started
    /// talking first (go watch for barge-in instead).
    async fn capture(&mut self, timeout: Duration, already: Vec<f32>) -> Option<Vec<f32>> {
        self.mic.drain();
        let mut deadline = Instant::now() + timeout;
        let mut rec: Vec<f32> = Vec::new();
        let mut preroll: Vec<f32> = Vec::new();
        let pre_max = (RATE as f32 * 0.6) as usize;
        let mut started = false;
        let mut last_speech = Instant::now();
        self.vad.reset();
        if !already.is_empty() {
            self.vad.accept(&already);
            rec = already;
            started = true;
            deadline = deadline.max(Instant::now() + Duration::from_secs(15));
            self.mouth.voice.hold(true);
        }
        while Instant::now() < deadline {
            tokio::time::sleep(POLL).await;
            if !started && self.mouth.is_speaking() {
                return None;
            }
            let buf = self.mic.drain();
            if buf.is_empty() {
                continue;
            }
            self.vad.accept(&buf);
            if self.vad.speaking() {
                if !started {
                    rec.extend_from_slice(&preroll);
                    deadline = deadline.max(Instant::now() + Duration::from_secs(15));
                    self.mouth.voice.hold(true); // don't talk over the user
                }
                started = true;
                last_speech = Instant::now();
            }
            if started {
                rec.extend_from_slice(&buf);
                if last_speech.elapsed() >= SILENCE_END {
                    break;
                }
            } else {
                preroll.extend_from_slice(&buf);
                if preroll.len() > pre_max {
                    let n = preroll.len() - pre_max;
                    preroll.drain(..n);
                }
            }
        }
        self.mouth.voice.hold(false);
        Some(if started { rec } else { Vec::new() })
    }
}

impl Ears for VoiceEars {
    fn barged(&self) -> bool {
        self.barged
    }

    async fn next(&mut self, mode: Mode, timeout: Option<Duration>) -> Heard {
        if mode == Mode::Sleeping {
            return self.wait_for_wake().await;
        }
        let timeout = timeout.unwrap_or(Duration::from_secs(8));
        self.barged = false;
        loop {
            let mut so_far = Vec::new();
            if self.mouth.is_speaking() {
                match self.barge_in_monitor().await {
                    Some(v) => {
                        so_far = v;
                        self.barged = true;
                    }
                    None => {
                        // finished naturally — let the room's echo die down
                        tokio::time::sleep(POST_SPEECH_GUARD).await;
                        self.mic.drain();
                    }
                }
                self.last_spoke = Instant::now();
            }
            let Some(utt) = self.capture(timeout, so_far).await else { continue };
            if utt.is_empty() {
                return Heard::Silence;
            }
            let mut t = tokio::task::block_in_place(|| self.recog.command(&utt));
            println!("[🎯 HEARD] {:?} (confidence {:.2}, unsure {:.2})", t.text, t.confidence, t.unsure);
            // A sentence that trails off ("send hi, to…") — the speaker paused
            // mid-thought.  Wait a little for the rest instead of acting on
            // half of it (live: "send high, do," pressed Cmd+Enter).
            for _ in 0..2 {
                if !brain::normalize::sounds_unfinished(&t.text) {
                    break;
                }
                let Some(more) = self.capture(Duration::from_secs(3), Vec::new()).await else { break };
                if more.is_empty() {
                    break;
                }
                let t2 = tokio::task::block_in_place(|| self.recog.command(&more));
                if t2.text.trim().is_empty() {
                    break;
                }
                println!("[➕ CONTINUED] {:?}", t2.text);
                t.text = format!("{} {}", t.text.trim_end_matches([',', '-']).trim(), t2.text.trim());
                t.confidence = t.confidence.min(t2.confidence);
                t.unsure = t.unsure.max(t2.unsure);
            }
            return Heard::Speech { text: t.text, confidence: t.confidence, unsure: t.unsure };
        }
    }
}
