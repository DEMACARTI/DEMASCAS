//! Microphone and speaker via cpal (CoreAudio on macOS, ALSA/PipeWire on
//! Linux).  Each stream lives on its own thread (cpal streams aren't Send);
//! the rest of DEMASCAS talks to them through shared buffers.

use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};

use crate::aec::EchoCanceller;
use crate::speex::Resampler;

/// Everything the mic hears, as 16 kHz mono.
pub const RATE: u32 = 16_000;

struct MicShared {
    buf: Mutex<VecDeque<f32>>,
    /// When the newest sample in `buf` was captured.
    last: Mutex<Instant>,
    alive: AtomicU64,
}

pub struct Mic {
    shared: Arc<MicShared>,
    stop: Arc<AtomicBool>,
    pub device_name: String,
}

fn spawn_stream<F>(name: &str, build: F) -> Result<String, String>
where
    F: FnOnce() -> Result<(cpal::Stream, String), String> + Send + 'static,
{
    let (tx, rx) = mpsc::channel();
    let stop = Arc::new(AtomicBool::new(false));
    let s2 = stop.clone();
    thread::Builder::new()
        .name(name.into())
        .spawn(move || match build() {
            Ok((stream, desc)) => {
                if let Err(e) = stream.play() {
                    let _ = tx.send(Err(format!("can't start: {e}")));
                    return;
                }
                let _ = tx.send(Ok(desc));
                while !s2.load(Ordering::Relaxed) {
                    thread::park_timeout(Duration::from_millis(500));
                }
                drop(stream);
            }
            Err(e) => {
                let _ = tx.send(Err(e));
            }
        })
        .map_err(|e| e.to_string())?;
    rx.recv_timeout(Duration::from_secs(10)).map_err(|_| "audio device didn't answer".to_string())?
}

impl Mic {
    /// Open the default input device.
    pub fn open() -> Result<Self, String> {
        let shared = Arc::new(MicShared { buf: Mutex::new(VecDeque::new()), last: Mutex::new(Instant::now()), alive: AtomicU64::new(0) });
        let sh = shared.clone();
        let device_name = spawn_stream("mic", move || {
            let host = cpal::default_host();
            let dev = host.default_input_device().ok_or("no microphone")?;
            let sup = dev.default_input_config().map_err(|e| format!("mic config: {e}"))?;
            let channels = sup.channels() as usize;
            let rate = sup.sample_rate();
            let fmt = sup.sample_format();
            let config: cpal::StreamConfig = sup.into();
            let mut rs = Resampler::new(rate, RATE, 5).ok_or("resampler")?;
            let mut mono = Vec::new();
            let mut on_audio = move |frames: &mut dyn Iterator<Item = f32>, latency: Duration| {
                mono.clear();
                let mut acc = 0.0;
                for (i, s) in frames.enumerate() {
                    acc += s;
                    if (i + 1) % channels == 0 {
                        mono.push(acc / channels as f32);
                        acc = 0.0;
                    }
                }
                let out = rs.process(&mono);
                let mut b = sh.buf.lock().unwrap();
                b.extend(out);
                let cap = RATE as usize * 30;
                if b.len() > cap {
                    let n = b.len() - cap;
                    b.drain(..n);
                }
                *sh.last.lock().unwrap() = Instant::now().checked_sub(latency).unwrap_or_else(Instant::now);
                sh.alive.fetch_add(1, Ordering::Relaxed);
            };
            let err = |e| eprintln!("[!] mic stream error: {e}");
            let latency = |info: &cpal::InputCallbackInfo| {
                let t = info.timestamp();
                t.callback.duration_since(t.capture)
            };
            let stream = match fmt {
                cpal::SampleFormat::F32 => dev.build_input_stream(config.clone(), move |d: &[f32], info: &cpal::InputCallbackInfo| on_audio(&mut d.iter().copied(), latency(info)), err, None),
                cpal::SampleFormat::I16 => dev.build_input_stream(config.clone(), move |d: &[i16], info: &cpal::InputCallbackInfo| on_audio(&mut d.iter().map(|&s| s as f32 / 32768.0), latency(info)), err, None),
                other => return Err(format!("unsupported mic sample format {other:?}")),
            }
            .map_err(|e| format!("can't open microphone: {e}"))?;
            Ok((stream, format!("{} Hz × {channels}", rate)))
        })?;
        Ok(Self { shared, stop: Arc::new(AtomicBool::new(false)), device_name })
    }

    /// Everything heard since the last drain.
    pub fn drain(&self) -> Vec<f32> {
        self.shared.buf.lock().unwrap().drain(..).collect()
    }

    /// Like [`drain`], plus when the last returned sample was captured.
    pub fn drain_timed(&self) -> (Vec<f32>, Instant) {
        let v: Vec<f32> = self.shared.buf.lock().unwrap().drain(..).collect();
        (v, *self.shared.last.lock().unwrap())
    }

    /// Callbacks so far — stays 0 if the OS gives no mic audio (permission).
    pub fn callbacks(&self) -> u64 {
        self.shared.alive.load(Ordering::Relaxed)
    }
}

impl Drop for Mic {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
    }
}

struct SpeakerShared {
    queue: Mutex<VecDeque<f32>>,
    /// RMS of what was just played (for barge-in heuristics).
    level: Mutex<f32>,
    aec: Mutex<Option<Arc<EchoCanceller>>>,
}

/// The speaker: queue samples, they play; `stop()` silences at once.
pub struct Speaker {
    shared: Arc<SpeakerShared>,
    pub rate: u32,
    resamplers: Mutex<Vec<Resampler>>,
}

impl Speaker {
    pub fn open() -> Result<Self, String> {
        let shared = Arc::new(SpeakerShared { queue: Mutex::new(VecDeque::new()), level: Mutex::new(0.0), aec: Mutex::new(None) });
        let sh = shared.clone();
        let (rtx, rrx) = mpsc::channel();
        spawn_stream("speaker", move || {
            let host = cpal::default_host();
            let dev = host.default_output_device().ok_or("no speaker")?;
            let sup = dev.default_output_config().map_err(|e| format!("speaker config: {e}"))?;
            let channels = sup.channels() as usize;
            let rate = sup.sample_rate();
            let config: cpal::StreamConfig = sup.into();
            let _ = rtx.send(rate);
            let mut mono = Vec::new();
            let stream = dev
                .build_output_stream(
                    config,
                    move |out: &mut [f32], info: &cpal::OutputCallbackInfo| {
                        let frames = out.len() / channels;
                        mono.clear();
                        {
                            let mut q = sh.queue.lock().unwrap();
                            for _ in 0..frames {
                                mono.push(q.pop_front().unwrap_or(0.0));
                            }
                        }
                        for (i, f) in out.chunks_mut(channels).enumerate() {
                            f.fill(mono[i]);
                        }
                        let e = (mono.iter().map(|v| v * v).sum::<f32>() / frames.max(1) as f32).sqrt();
                        *sh.level.lock().unwrap() = e;
                        if e > 0.0 {
                            let t = info.timestamp();
                            let ahead = t.playback.duration_since(t.callback);
                            if let Some(aec) = sh.aec.lock().unwrap().as_ref() {
                                aec.push_reference(&mono, rate, Instant::now() + ahead);
                            }
                        }
                    },
                    |e| eprintln!("[!] speaker stream error: {e}"),
                    None,
                )
                .map_err(|e| format!("can't open speaker: {e}"))?;
            Ok((stream, format!("{rate} Hz × {channels}")))
        })?;
        let rate = rrx.recv_timeout(Duration::from_secs(5)).unwrap_or(48_000);
        Ok(Self { shared, rate, resamplers: Mutex::new(Vec::new()) })
    }

    /// Feed what's played to the echo canceller.
    pub fn set_echo_canceller(&self, aec: Arc<EchoCanceller>) {
        *self.shared.aec.lock().unwrap() = Some(aec);
    }

    /// Queue `samples` (mono, at `rate` Hz) for playback.
    pub fn play(&self, samples: &[f32], rate: u32) {
        let pcm = if rate == self.rate {
            samples.to_vec()
        } else {
            let mut rs = self.resamplers.lock().unwrap();
            if !rs.iter().any(|r| r.from == rate) {
                if let Some(r) = Resampler::new(rate, self.rate, 5) {
                    rs.push(r);
                }
            }
            rs.iter_mut().find(|r| r.from == rate).map(|r| r.process(samples)).unwrap_or_default()
        };
        self.shared.queue.lock().unwrap().extend(pcm);
    }

    /// Silence now (interrupt).
    pub fn stop(&self) {
        self.shared.queue.lock().unwrap().clear();
    }

    /// Seconds of audio still queued.
    pub fn queued_secs(&self) -> f32 {
        self.shared.queue.lock().unwrap().len() as f32 / self.rate as f32
    }

    pub fn is_playing(&self) -> bool {
        !self.shared.queue.lock().unwrap().is_empty() || *self.shared.level.lock().unwrap() > 0.0
    }

    pub fn level(&self) -> f32 {
        *self.shared.level.lock().unwrap()
    }
}
