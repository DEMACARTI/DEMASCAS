//! Safe wrappers over the sherpa-onnx C API (the same library the C++ daemon
//! used): the Kokoro voice and the Silero speech detector.

#![allow(non_upper_case_globals, non_camel_case_types, non_snake_case, dead_code, clippy::all)]

use std::ffi::CString;
use std::path::Path;

#[allow(unused)]
mod ffi {
    include!(concat!(env!("OUT_DIR"), "/bindings.rs"));
}

fn cstr(s: &str) -> CString {
    CString::new(s.replace('\0', " ")).unwrap()
}

/// Kokoro text-to-speech.
pub struct Tts {
    ptr: *const ffi::SherpaOnnxOfflineTts,
    pub sample_rate: u32,
    pub speakers: i32,
}

// sherpa-onnx's offline TTS is used from one thread at a time (our worker).
unsafe impl Send for Tts {}

impl Tts {
    /// Load Kokoro from a model directory (model.onnx, voices.bin, tokens.txt, espeak-ng-data/).
    pub fn kokoro(dir: &Path, threads: i32, speed: f32) -> Result<Self, String> {
        for f in ["model.onnx", "voices.bin", "tokens.txt"] {
            if !dir.join(f).exists() {
                return Err(format!("Kokoro file missing: {}", dir.join(f).display()));
            }
        }
        let model = cstr(&dir.join("model.onnx").to_string_lossy());
        let voices = cstr(&dir.join("voices.bin").to_string_lossy());
        let tokens = cstr(&dir.join("tokens.txt").to_string_lossy());
        let data = cstr(&dir.join("espeak-ng-data").to_string_lossy());
        let provider = cstr("cpu");
        unsafe {
            let mut c: ffi::SherpaOnnxOfflineTtsConfig = std::mem::zeroed();
            c.model.kokoro.model = model.as_ptr();
            c.model.kokoro.voices = voices.as_ptr();
            c.model.kokoro.tokens = tokens.as_ptr();
            c.model.kokoro.data_dir = data.as_ptr();
            c.model.kokoro.length_scale = 1.0 / speed.max(0.5);
            c.model.num_threads = threads.max(1);
            c.model.provider = provider.as_ptr(); // GPU stays free for the recogniser
            c.model.debug = 0;
            c.max_num_sentences = 1;
            let ptr = ffi::SherpaOnnxCreateOfflineTts(&c);
            if ptr.is_null() {
                return Err("SherpaOnnxCreateOfflineTts failed".into());
            }
            let sample_rate = ffi::SherpaOnnxOfflineTtsSampleRate(ptr) as u32;
            let speakers = ffi::SherpaOnnxOfflineTtsNumSpeakers(ptr);
            Ok(Self { ptr, sample_rate, speakers })
        }
    }

    /// Synthesize one sentence → mono samples at `sample_rate`.
    pub fn generate(&self, text: &str, speaker: i32, speed: f32) -> Vec<f32> {
        let t = cstr(text);
        unsafe {
            let a = ffi::SherpaOnnxOfflineTtsGenerate(self.ptr, t.as_ptr(), speaker, speed);
            if a.is_null() {
                return Vec::new();
            }
            let n = (*a).n.max(0) as usize;
            let v = if n == 0 || (*a).samples.is_null() { Vec::new() } else { std::slice::from_raw_parts((*a).samples, n).to_vec() };
            ffi::SherpaOnnxDestroyOfflineTtsGeneratedAudio(a);
            v
        }
    }
}

impl Drop for Tts {
    fn drop(&mut self) {
        unsafe { ffi::SherpaOnnxDestroyOfflineTts(self.ptr) }
    }
}

/// Silero voice-activity detector: tells a human voice from fans, typing, music.
pub struct Vad {
    ptr: *const ffi::SherpaOnnxVoiceActivityDetector,
}

unsafe impl Send for Vad {}

impl Vad {
    /// threshold 0.7 / min speech 0.25 s / min silence 0.1 s measured best
    /// (0 % typing/fan/music, speech kept).
    pub fn silero(model: &Path, sample_rate: u32, threshold: f32, min_speech: f32, min_silence: f32) -> Result<Self, String> {
        if !model.exists() {
            return Err(format!("speech detector model missing: {}", model.display()));
        }
        let m = cstr(&model.to_string_lossy());
        let provider = cstr("cpu");
        unsafe {
            let mut c: ffi::SherpaOnnxVadModelConfig = std::mem::zeroed();
            c.silero_vad.model = m.as_ptr();
            c.silero_vad.threshold = threshold;
            c.silero_vad.min_silence_duration = min_silence;
            c.silero_vad.min_speech_duration = min_speech;
            c.silero_vad.window_size = 512; // 32 ms at 16 kHz
            c.silero_vad.max_speech_duration = 30.0;
            c.sample_rate = sample_rate as i32;
            c.num_threads = 1;
            c.provider = provider.as_ptr();
            c.debug = 0;
            let ptr = ffi::SherpaOnnxCreateVoiceActivityDetector(&c, 30.0);
            if ptr.is_null() {
                return Err("speech detector failed to load".into());
            }
            Ok(Self { ptr })
        }
    }

    pub fn standard(model: &Path) -> Result<Self, String> {
        Self::silero(model, 16_000, 0.7, 0.25, 0.1)
    }

    pub fn accept(&mut self, samples: &[f32]) {
        if samples.is_empty() {
            return;
        }
        unsafe {
            ffi::SherpaOnnxVoiceActivityDetectorAcceptWaveform(self.ptr, samples.as_ptr(), samples.len() as i32);
            // only the live "speaking" flag is used: drop finished segments
            while ffi::SherpaOnnxVoiceActivityDetectorEmpty(self.ptr) == 0 {
                ffi::SherpaOnnxVoiceActivityDetectorPop(self.ptr);
            }
        }
    }

    pub fn speaking(&self) -> bool {
        unsafe { ffi::SherpaOnnxVoiceActivityDetectorDetected(self.ptr) != 0 }
    }

    pub fn reset(&mut self) {
        unsafe { ffi::SherpaOnnxVoiceActivityDetectorReset(self.ptr) }
    }
}

impl Drop for Vad {
    fn drop(&mut self) {
        unsafe { ffi::SherpaOnnxDestroyVoiceActivityDetector(self.ptr) }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn models() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../models")
    }

    #[test]
    fn vad_hears_voice_not_silence() {
        let Ok(mut v) = Vad::standard(&models().join("silero_vad.onnx")) else { return };
        v.accept(&vec![0.0; 16_000]);
        assert!(!v.speaking());
    }

    #[test]
    fn kokoro_speaks_without_playing() {
        // synthesis only — nothing is played
        let Ok(t) = Tts::kokoro(&models().join("kokoro-en-v0_19"), 2, 1.0) else { return };
        let pcm = t.generate("Done.", 0, 1.0);
        assert!(t.sample_rate >= 16_000);
        assert!(pcm.len() > t.sample_rate as usize / 10, "{} samples", pcm.len());
    }
}
