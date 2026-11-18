//! Barge-in: is the user talking over DEMASCAS?  (ported from `vad.hpp`)
//!
//! Silero is nearly volume-blind, so the faint residue of our own voice
//! after echo cancellation can still look like speech.  The user's voice
//! survives cancellation (keeps ≥ ~45 % of the mic energy even 6 dB below
//! the speakers); echo residue keeps ~10 %.  Before the canceller has
//! converged, fall back to "speak over the speakers".

use std::collections::VecDeque;

pub struct BargeIn {
    window: usize,
    strong_needed: usize,
    min_keep: f32,
    min_level: f32,
    legacy: f32,
    strong: VecDeque<bool>,
}

impl Default for BargeIn {
    fn default() -> Self {
        Self { window: 6, strong_needed: 2, min_keep: 0.4, min_level: 0.008, legacy: 0.18, strong: VecDeque::new() }
    }
}

impl BargeIn {
    /// One ~50 ms poll.
    pub fn update(&mut self, mic_rms: f32, clean_rms: f32, speech_in_clean: bool, aec_converged: bool, raw_speech: bool) -> bool {
        if !aec_converged {
            self.strong.clear();
            return raw_speech && mic_rms >= self.legacy;
        }
        let strong = clean_rms >= self.min_level && clean_rms >= self.min_keep * mic_rms.max(1e-6);
        self.strong.push_back(strong);
        while self.strong.len() > self.window {
            self.strong.pop_front();
        }
        speech_in_clean && self.strong.iter().filter(|s| **s).count() >= self.strong_needed
    }

    pub fn reset(&mut self) {
        self.strong.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn echo_residue_never_triggers_but_a_voice_does() {
        let mut b = BargeIn::default();
        // converged: mic 0.1, residue 0.01 (10 %) with VAD fooled → no
        for _ in 0..20 {
            assert!(!b.update(0.1, 0.01, true, true, true));
        }
        // the user talks: half the energy survives cancellation
        b.reset();
        let hits: Vec<bool> = (0..4).map(|_| b.update(0.1, 0.05, true, true, true)).collect();
        assert!(hits.contains(&true));
        // not converged: only a loud voice over the speakers counts
        b.reset();
        assert!(!b.update(0.1, 0.1, true, false, true));
        assert!(b.update(0.25, 0.25, true, false, true));
    }
}
