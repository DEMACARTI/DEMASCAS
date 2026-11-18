//! Speech recognition — whisper.cpp through whisper-rs (Metal on Apple
//! silicon), ported from `daemon/src/whisper_engine.cpp`:
//! * tiny model for the wake word, base model for commands;
//! * adaptive audio window (50 frames/s + margin) — ~90 ms vs ~250 ms;
//! * a vocabulary prompt (taught words, learned apps);
//! * the recogniser's own confidence, so garbled speech is never acted on.

use std::path::Path;

use whisper_rs::{FullParams, SamplingStrategy, WhisperContext, WhisperContextParameters, WhisperState};

pub const RATE: usize = 16_000;
/// whisper ignores clips shorter than ~1 s — pad to this.
const MIN_SEC: f32 = 1.5;

#[derive(Debug, Clone, Default, PartialEq)]
pub struct Transcript {
    pub text: String,
    /// Mean token probability (0–1).
    pub confidence: f32,
    /// Share of tokens with probability < 0.3.
    pub unsure: f32,
}

struct Model {
    ctx: WhisperContext,
    state: WhisperState,
}

impl Model {
    fn load(path: &Path) -> Result<Self, String> {
        let p = path.to_str().ok_or("bad model path")?;
        let ctx = WhisperContext::new_with_params(p, WhisperContextParameters::default())
            .map_err(|e| format!("can't load {}: {e}", path.display()))?;
        let state = ctx.create_state().map_err(|e| format!("whisper state: {e}"))?;
        Ok(Self { ctx, state })
    }
}

pub struct Recognizer {
    tiny: Model,
    base: Model,
    prompt: String,
    threads: i32,
}

/// "X. X." / "X? X?" → "X." (whisper repetition loops).
pub fn collapse_repeated_sentences(text: &str) -> String {
    let mut parts = Vec::new();
    let mut cur = String::new();
    for c in text.chars() {
        cur.push(c);
        if matches!(c, '.' | '?' | '!') {
            parts.push(std::mem::take(&mut cur));
        }
    }
    if !cur.is_empty() {
        parts.push(cur);
    }
    if parts.len() < 2 {
        return text.to_string();
    }
    let norm = |s: &str| s.chars().filter(|c| c.is_alphanumeric() || *c == ' ').flat_map(char::to_lowercase).collect::<String>().trim().to_string();
    let (mut out, mut prev) = (String::new(), String::new());
    for p in parts {
        let n = norm(&p);
        if !n.is_empty() && n == prev {
            continue;
        }
        out.push_str(&p);
        prev = n;
    }
    out
}

/// Encoder frames for a clip: 50/s + 256 margin, multiple of 64, ≥ 512;
/// 0 (= the full 30 s window) once it would reach 1500.
pub fn adaptive_ctx(samples: usize) -> i32 {
    let frames = (samples as f32 / RATE as f32 * 50.0) as i32 + 256;
    let frames = (frames + 63) / 64 * 64;
    let ctx = frames.max(512);
    if ctx >= 1500 { 0 } else { ctx }
}

impl Recognizer {
    pub fn load(tiny: &Path, base: &Path, threads: i32) -> Result<Self, String> {
        Ok(Self { tiny: Model::load(tiny)?, base: Model::load(base)?, prompt: String::new(), threads: threads.max(1) })
    }

    pub fn set_prompt(&mut self, prompt: &str) {
        self.prompt = prompt.to_string();
    }

    fn run_once(model: &mut Model, pcm: &[f32], audio_ctx: i32, prompt: &str, threads: i32) -> Transcript {
        let mut p = FullParams::new(SamplingStrategy::Greedy { best_of: 1 });
        p.set_print_progress(false);
        p.set_print_timestamps(false);
        p.set_print_realtime(false);
        p.set_print_special(false);
        p.set_single_segment(true);
        p.set_language(Some("en"));
        p.set_n_threads(threads);
        p.set_no_context(true);
        p.set_audio_ctx(audio_ctx);
        if !prompt.is_empty() {
            p.set_initial_prompt(prompt);
        }
        if model.state.full(p, pcm).is_err() {
            return Transcript::default();
        }
        let eot = model.ctx.token_eot();
        let (mut text, mut sum, mut n, mut unsure) = (String::new(), 0.0f64, 0usize, 0usize);
        for seg in model.state.as_iter() {
            if let Ok(t) = seg.to_str_lossy() {
                text.push_str(&t);
            }
            for i in 0..seg.n_tokens() {
                let Some(tok) = seg.get_token(i) else { continue };
                if tok.token_id() >= eot {
                    continue; // special tokens
                }
                let pr = tok.token_probability();
                sum += pr as f64;
                unsure += (pr < 0.3) as usize;
                n += 1;
            }
        }
        Transcript {
            text: text.trim().to_string(),
            confidence: if n > 0 { (sum / n as f64) as f32 } else { 0.0 },
            unsure: if n > 0 { unsure as f32 / n as f32 } else { 1.0 },
        }
    }

    fn run(model: &mut Model, pcm: &[f32], adaptive: bool, prompt: &str, threads: i32) -> Transcript {
        if pcm.is_empty() {
            return Transcript::default();
        }
        let min = (MIN_SEC * RATE as f32) as usize;
        let mut buf = pcm.to_vec();
        if buf.len() < min {
            buf.resize(min, 0.0);
        }
        let ctx = if adaptive { adaptive_ctx(buf.len()) } else { 0 };
        let mut t = Self::run_once(model, &buf, ctx, prompt, threads);
        if ctx != 0 && t.text.is_empty() {
            t = Self::run_once(model, &buf, 0, prompt, threads); // full window fallback
        }
        t.text = collapse_repeated_sentences(&t.text);
        t
    }

    /// The wake-word check (tiny model, full window — reduced windows loop on tiny).
    pub fn wake(&mut self, pcm: &[f32]) -> String {
        let prompt = self.prompt.clone();
        Self::run(&mut self.tiny, pcm, false, &prompt, self.threads).text
    }

    /// A command (base model, adaptive window).
    pub fn command(&mut self, pcm: &[f32]) -> Transcript {
        let prompt = self.prompt.clone();
        Self::run(&mut self.base, pcm, true, &prompt, self.threads)
    }

    /// Build Metal graphs now, not on the first command.
    pub fn warm_up(&mut self) {
        let silence = vec![0.0f32; (MIN_SEC * RATE as f32) as usize];
        let _ = self.wake(&silence);
        let _ = self.command(&silence);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    #[test]
    fn helpers() {
        assert_eq!(collapse_repeated_sentences("Open Discord. Open Discord."), "Open Discord.");
        assert_eq!(collapse_repeated_sentences("Hi. Bye."), "Hi. Bye.");
        assert_eq!(adaptive_ctx(16_000), 320.max(512));
        assert_eq!(adaptive_ctx(16_000 * 30), 0);
    }

    /// Reads a little-endian float32 WAV (as written by `say --data-format=LEF32@16000`).
    fn read_f32_wav(path: &std::path::Path) -> Vec<f32> {
        let b = std::fs::read(path).unwrap();
        let mut i = 12;
        while i + 8 <= b.len() {
            let id = &b[i..i + 4];
            let len = u32::from_le_bytes(b[i + 4..i + 8].try_into().unwrap()) as usize;
            if id == b"data" {
                return b[i + 8..(i + 8 + len).min(b.len())].chunks_exact(4).map(|c| f32::from_le_bytes(c.try_into().unwrap())).collect();
            }
            i += 8 + len + (len & 1);
        }
        Vec::new()
    }

    /// End-to-end on a synthesized clip (written to a file, never played).
    #[test]
    fn hears_a_command() {
        let models = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../models");
        let (tiny, base) = (models.join("ggml-tiny.bin"), models.join("ggml-base.bin"));
        if !tiny.exists() || !base.exists() || !cfg!(target_os = "macos") {
            return;
        }
        let wav = std::env::temp_dir().join("demascas-speech-test.wav");
        let ok = std::process::Command::new("say")
            .args(["-o", wav.to_str().unwrap(), "--data-format=LEF32@16000", "--file-format=WAVE", "open Discord please"])
            .status().map(|s| s.success()).unwrap_or(false);
        if !ok {
            return;
        }
        let pcm = read_f32_wav(&wav);
        let mut r = Recognizer::load(&tiny, &base, 4).unwrap();
        let t = r.command(&pcm);
        assert!(t.text.to_lowercase().contains("discord"), "{t:?}");
        assert!(t.confidence > 0.5, "{t:?}");
    }
}
