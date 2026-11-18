//! Audio for DEMASCAS: microphone and speaker ([`devices`]), echo
//! cancellation ([`aec`]) and barge-in detection ([`bargein`]).

pub mod aec;
pub mod bargein;
pub mod devices;
pub mod speex;

pub use aec::EchoCanceller;
pub use bargein::BargeIn;
pub use devices::{Mic, Speaker, RATE};

/// Root-mean-square level of a chunk.
pub fn rms(x: &[f32]) -> f32 {
    if x.is_empty() {
        return 0.0;
    }
    (x.iter().map(|v| v * v).sum::<f32>() / x.len() as f32).sqrt()
}
