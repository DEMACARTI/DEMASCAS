//! Acoustic echo cancellation (ported from `daemon/src/aec.cpp`): remove
//! DEMASCAS's own voice from the mic so the user can interrupt at a normal
//! speaking volume.
//!
//! The speaker pushes every chunk it plays, stamped with when it will be
//! heard; the mic side takes the matching reference for each 10 ms frame and
//! speexdsp subtracts the echo.  "Converged" = ≥ 2 s of our speech heard AND
//! a measured echo reduction ≥ 12 dB; it un-latches after 2 s below 6 dB
//! (real speakers can collapse it — DEMASCAS then interrupted itself).

use std::sync::Mutex;
use std::time::Instant;

use crate::speex::{Echo, Resampler};

struct Inner {
    echo: Echo,
    resampler: Option<Resampler>,
    ring: Vec<f32>,
    written_upto: i64,
    adapted: i64,
    mic_e: f64,
    out_e: f64,
    erle_db: f32,
    converged: bool,
    weak: i64,
    pending: Vec<f32>,
    pending_start: i64,
}

pub struct EchoCanceller {
    rate: u32,
    frame: usize,
    residual: bool,
    epoch: Instant,
    inner: Mutex<Inner>,
}

fn to_s16(x: f32) -> i16 {
    (x * 32767.0).clamp(-32768.0, 32767.0) as i16
}

impl EchoCanceller {
    pub fn new(rate: u32, tail_ms: u32, suppress_active_db: i32, residual: bool) -> Option<Self> {
        let frame = (rate / 100) as usize;
        let echo = Echo::new(frame, rate, tail_ms, suppress_active_db)?;
        Some(Self {
            rate, frame, residual, epoch: Instant::now(),
            inner: Mutex::new(Inner {
                echo, resampler: None, ring: vec![0.0; rate as usize * 4], written_upto: 0, adapted: 0,
                mic_e: 0.0, out_e: 0.0, erle_db: 0.0, converged: false, weak: 0, pending: Vec::new(), pending_start: 0,
            }),
        })
    }

    /// The default DEMASCAS settings (16 kHz, 200 ms tail, -5 dB double-talk).
    pub fn standard() -> Option<Self> {
        Self::new(16_000, 200, -5, true)
    }

    fn index_of(&self, t: Instant) -> i64 {
        let us = t.saturating_duration_since(self.epoch).as_micros() as i64;
        us * self.rate as i64 / 1_000_000
    }

    /// Speaker side: `samples` at `rate` Hz that start playing at `when`.
    pub fn push_reference(&self, samples: &[f32], rate: u32, when: Instant) {
        if samples.is_empty() || rate == 0 {
            return;
        }
        let start = self.index_of(when);
        let mut g = self.inner.lock().unwrap();
        let res;
        let src: &[f32] = if rate != self.rate {
            if g.resampler.as_ref().is_none_or(|r| r.from != rate) {
                g.resampler = Resampler::new(rate, self.rate, 5);
            }
            res = g.resampler.as_mut().map(|r| r.process(samples)).unwrap_or_default();
            &res
        } else {
            samples
        };
        let cap = g.ring.len() as i64;
        for (i, v) in src.iter().enumerate() {
            let idx = start + i as i64;
            g.ring[idx.rem_euclid(cap) as usize] = *v;
        }
        g.written_upto = g.written_upto.max(start + src.len() as i64);
    }

    /// Mic side: clean 16 kHz samples whose LAST sample was captured at
    /// `captured_end`.  Returns as many samples as full frames allow.
    pub fn process(&self, mic: &[f32], captured_end: Instant) -> Vec<f32> {
        if mic.is_empty() {
            return Vec::new();
        }
        let end = self.index_of(captured_end);
        let start = end - mic.len() as i64;
        let mut g = self.inner.lock().unwrap();
        if g.pending.is_empty() || g.pending_start + g.pending.len() as i64 != start {
            g.pending.clear(); // gap or first call: realign
            g.pending_start = start;
        }
        g.pending.extend_from_slice(mic);
        let f = self.frame;
        let (mut m, mut r, mut o) = (vec![0i16; f], vec![0i16; f], vec![0i16; f]);
        let mut out = Vec::with_capacity(g.pending.len());
        let mut pos = 0usize;
        let cap = g.ring.len() as i64;
        while g.pending.len() - pos >= f {
            let mut live = false;
            for k in 0..f {
                m[k] = to_s16(g.pending[pos + k]);
                let idx = g.pending_start + (pos + k) as i64;
                let rv = if idx < 0 || idx >= g.written_upto || idx < g.written_upto - cap { 0.0 } else { g.ring[idx.rem_euclid(cap) as usize] };
                live |= rv.abs() > 1e-4;
                r[k] = to_s16(rv);
            }
            let residual = self.residual;
            g.echo.cancel(&m, &r, &mut o, residual);
            out.extend(o.iter().map(|&x| x as f32 / 32768.0));
            if live {
                g.adapted += f as i64;
                let me: f64 = m.iter().map(|&x| x as f64 * x as f64).sum();
                let oe: f64 = o.iter().map(|&x| x as f64 * x as f64).sum();
                g.mic_e = 0.98 * g.mic_e + 0.02 * me;
                g.out_e = 0.98 * g.out_e + 0.02 * oe;
                g.erle_db = (10.0 * ((g.mic_e + 1.0) / (g.out_e + 1.0)).log10()) as f32;
                if !g.converged && g.adapted >= 2 * self.rate as i64 && g.erle_db >= 12.0 {
                    g.converged = true;
                    g.weak = 0;
                } else if g.converged {
                    g.weak = if g.erle_db < 6.0 { g.weak + f as i64 } else { 0 };
                    if g.weak >= 2 * self.rate as i64 {
                        g.converged = false;
                        g.weak = 0;
                    }
                }
            }
            pos += f;
        }
        g.pending.drain(..pos);
        g.pending_start += pos as i64;
        out
    }

    pub fn converged(&self) -> bool {
        self.inner.lock().unwrap().converged
    }

    pub fn echo_reduction_db(&self) -> f32 {
        self.inner.lock().unwrap().erle_db
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    fn rms(x: &[f32]) -> f32 {
        (x.iter().map(|v| v * v).sum::<f32>() / x.len().max(1) as f32).sqrt()
    }

    /// A speech-like signal (several harmonics, amplitude-modulated).
    fn voice(n: usize, rate: f32, seed: f32) -> Vec<f32> {
        (0..n).map(|i| {
            let t = i as f32 / rate;
            let env = 0.5 + 0.5 * (t * 3.1 + seed).sin().abs();
            env * 0.3 * ((t * 180.0 * 6.283).sin() + 0.5 * (t * 360.0 * 6.283 + seed).sin() + 0.25 * (t * 720.0 * 6.283).sin())
        }).collect()
    }

    #[test]
    fn cancels_its_own_voice_and_converges() {
        let aec = EchoCanceller::standard().unwrap();
        let rate = 16_000usize;
        let t0 = aec.epoch + Duration::from_millis(100);
        let refsig = voice(rate * 5, 16_000.0, 0.3);
        aec.push_reference(&refsig, 16_000, t0);
        // the mic hears it 30 ms later, 6 dB down, through a little "room"
        let delay = rate * 30 / 1000;
        let mut mic = vec![0.0f32; refsig.len() + delay];
        for i in 0..refsig.len() {
            mic[i + delay] += 0.5 * refsig[i];
            if i + delay + 40 < mic.len() {
                mic[i + delay + 40] += 0.15 * refsig[i];
            }
        }
        let mut cleaned = Vec::new();
        for (k, chunk) in mic.chunks(800).enumerate() {
            let end = t0 + Duration::from_micros(((k * 800 + chunk.len()) as u64) * 1_000_000 / rate as u64);
            cleaned.extend(aec.process(chunk, end));
        }
        let tail = rate * 2; // last 2 s, after adaptation
        let before = rms(&mic[mic.len() - tail..]);
        let after = rms(&cleaned[cleaned.len() - tail..]);
        let db = 20.0 * (before / after.max(1e-9)).log10();
        assert!(db >= 15.0, "echo reduction only {db:.1} dB");
        assert!(aec.converged(), "erle {}", aec.echo_reduction_db());
    }
}
