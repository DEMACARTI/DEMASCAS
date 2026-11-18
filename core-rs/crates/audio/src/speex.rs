//! Minimal safe wrappers over speexdsp: echo canceller, preprocessor
//! (residual-echo + noise suppression) and the band-limited resampler.

use std::os::raw::{c_int, c_void};

#[allow(non_camel_case_types)]
type spx_int16_t = i16;
#[allow(non_camel_case_types)]
type spx_uint32_t = u32;

const SPEEX_ECHO_SET_SAMPLING_RATE: c_int = 24;
const SPEEX_PREPROCESS_SET_DENOISE: c_int = 0;
const SPEEX_PREPROCESS_SET_ECHO_STATE: c_int = 24;
const SPEEX_PREPROCESS_SET_ECHO_SUPPRESS_ACTIVE: c_int = 22;

extern "C" {
    fn speex_echo_state_init(frame_size: c_int, filter_length: c_int) -> *mut c_void;
    fn speex_echo_state_destroy(st: *mut c_void);
    fn speex_echo_ctl(st: *mut c_void, request: c_int, ptr: *mut c_void) -> c_int;
    fn speex_echo_cancellation(st: *mut c_void, rec: *const spx_int16_t, play: *const spx_int16_t, out: *mut spx_int16_t);
    fn speex_preprocess_state_init(frame_size: c_int, sampling_rate: c_int) -> *mut c_void;
    fn speex_preprocess_state_destroy(st: *mut c_void);
    fn speex_preprocess_ctl(st: *mut c_void, request: c_int, ptr: *mut c_void) -> c_int;
    fn speex_preprocess_run(st: *mut c_void, x: *mut spx_int16_t) -> c_int;
    fn speex_resampler_init(nb_channels: spx_uint32_t, in_rate: spx_uint32_t, out_rate: spx_uint32_t, quality: c_int, err: *mut c_int) -> *mut c_void;
    fn speex_resampler_destroy(st: *mut c_void);
    fn speex_resampler_process_float(st: *mut c_void, channel_index: spx_uint32_t, input: *const f32, in_len: *mut spx_uint32_t, out: *mut f32, out_len: *mut spx_uint32_t) -> c_int;
}

/// speexdsp's adaptive echo filter + preprocessor, 16-bit frames.
pub struct Echo {
    echo: *mut c_void,
    pre: *mut c_void,
}

// The states are only touched behind the EchoCanceller's mutex.
unsafe impl Send for Echo {}

impl Echo {
    pub fn new(frame: usize, rate: u32, tail_ms: u32, suppress_active_db: i32) -> Option<Self> {
        unsafe {
            let echo = speex_echo_state_init(frame as c_int, (rate * tail_ms / 1000) as c_int);
            if echo.is_null() {
                return None;
            }
            let mut r = rate as c_int;
            speex_echo_ctl(echo, SPEEX_ECHO_SET_SAMPLING_RATE, &mut r as *mut _ as *mut c_void);
            let pre = speex_preprocess_state_init(frame as c_int, rate as c_int);
            speex_preprocess_ctl(pre, SPEEX_PREPROCESS_SET_ECHO_STATE, echo);
            let mut on: c_int = 1;
            speex_preprocess_ctl(pre, SPEEX_PREPROCESS_SET_DENOISE, &mut on as *mut _ as *mut c_void);
            // Gentle residual suppression during double-talk: the default
            // (-15 dB) also crushes the first word of an interruption.
            let mut active: c_int = suppress_active_db;
            speex_preprocess_ctl(pre, SPEEX_PREPROCESS_SET_ECHO_SUPPRESS_ACTIVE, &mut active as *mut _ as *mut c_void);
            Some(Self { echo, pre })
        }
    }

    pub fn cancel(&mut self, mic: &[i16], reference: &[i16], out: &mut [i16], residual: bool) {
        assert!(mic.len() == reference.len() && mic.len() == out.len());
        unsafe {
            speex_echo_cancellation(self.echo, mic.as_ptr(), reference.as_ptr(), out.as_mut_ptr());
            if residual {
                speex_preprocess_run(self.pre, out.as_mut_ptr());
            }
        }
    }
}

impl Drop for Echo {
    fn drop(&mut self) {
        unsafe {
            speex_preprocess_state_destroy(self.pre);
            speex_echo_state_destroy(self.echo);
        }
    }
}

/// Band-limited mono resampler (quality 0–10; 5 = speex's default).
pub struct Resampler {
    st: *mut c_void,
    pub from: u32,
    pub to: u32,
}

unsafe impl Send for Resampler {}

impl Resampler {
    pub fn new(from: u32, to: u32, quality: i32) -> Option<Self> {
        let mut err: c_int = 0;
        let st = unsafe { speex_resampler_init(1, from, to, quality, &mut err) };
        (!st.is_null() && err == 0).then_some(Self { st, from, to })
    }

    /// Resample a chunk (streaming: filter state carries over).
    pub fn process(&mut self, input: &[f32]) -> Vec<f32> {
        if self.from == self.to {
            return input.to_vec();
        }
        let mut out = vec![0.0f32; input.len() * self.to as usize / self.from as usize + 64];
        let mut in_len = input.len() as spx_uint32_t;
        let mut out_len = out.len() as spx_uint32_t;
        unsafe {
            speex_resampler_process_float(self.st, 0, input.as_ptr(), &mut in_len, out.as_mut_ptr(), &mut out_len);
        }
        out.truncate(out_len as usize);
        out
    }
}

impl Drop for Resampler {
    fn drop(&mut self) {
        unsafe { speex_resampler_destroy(self.st) }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resampler_keeps_duration() {
        let mut r = Resampler::new(48_000, 16_000, 5).unwrap();
        let tone: Vec<f32> = (0..48_000).map(|i| (i as f32 * 440.0 * 2.0 * std::f32::consts::PI / 48_000.0).sin() * 0.5).collect();
        let mut out = Vec::new();
        for c in tone.chunks(480) {
            out.extend(r.process(c));
        }
        assert!((out.len() as i32 - 16_000).abs() < 100, "{}", out.len());
    }
}
