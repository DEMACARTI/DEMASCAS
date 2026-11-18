//! Streaming client for an OpenAI-compatible local model server
//! (MLX `mlx_lm server`, llama.cpp `llama-server`, Ollama, LM Studio).
//!
//! Replaces the streaming half of `daemon/src/llm_client.cpp`:
//! * SSE token stream → sentences spoken as soon as they end;
//! * deadlines — no first word in 20 s or 45 s in total → stop and say so;
//! * repetition-loop cut; cancel at any moment;
//! * one timing line per call (`[LLM] prompt ≈N tok · first word …`), the
//!   same format as the C++ daemon so the exam compares them directly.

use std::fmt;
use std::time::{Duration, Instant};

use futures_util::StreamExt;
use serde_json::{json, Value};
use tokio_util::sync::CancellationToken;

pub mod sentences;

pub use sentences::{is_looping, split_sentence, SentenceStream};

/// When to give up instead of leaving the user in silence.
#[derive(Debug, Clone, Copy)]
pub struct Deadlines {
    pub first_word: Duration,
    pub total: Duration,
}

impl Default for Deadlines {
    fn default() -> Self {
        // The server can't stop a prefill it has started: giving up early only
        // wastes the work and queues the user's retry behind it (18 Nov log).
        Self { first_word: Duration::from_secs(40), total: Duration::from_secs(60) }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Stop {
    /// The model finished.
    Done,
    /// No first word in time (usually: reading a very long prompt).
    NoFirstWord,
    /// The answer ran past the total deadline.
    TooLong,
    /// Generation started repeating itself.
    Looped,
    Cancelled,
    /// Couldn't reach the server, or it returned an error.
    Error,
}

/// Where the time went — a slow FIRST word means the model re-read a long
/// prompt (prefix not cached); slow after that means it generated too much.
#[derive(Debug, Clone)]
pub struct Stats {
    pub prompt_tokens_est: usize,
    pub first_word: Option<Duration>,
    pub tokens: usize,
    pub total: Duration,
    pub stop: Stop,
    pub http_status: u16,
    pub error: String,
}

impl fmt::Display for Stats {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let first = self.first_word.unwrap_or(self.total).as_secs_f32();
        write!(
            f,
            "[LLM] prompt ≈{} tok · first word {:.3} s · {} tokens · {:.3} s total",
            self.prompt_tokens_est, first, self.tokens, self.total.as_secs_f32()
        )?;
        match self.stop {
            Stop::Looped => write!(f, " · repetition loop cut"),
            Stop::NoFirstWord => write!(f, " · gave up: no first word"),
            Stop::TooLong => write!(f, " · gave up: answer too long"),
            Stop::Cancelled => write!(f, " · cancelled"),
            Stop::Error => write!(f, " · error: {}", self.error),
            Stop::Done => Ok(()),
        }?;
        // A slow first word on a short prompt is usually the Mac swapping the
        // model in and out of memory, not the prompt — show it (live 18 Nov:
        // every call ~5× slower than the same prompt in text mode).
        if first > 1.5 {
            if let Some(mb) = swap_used_mb().filter(|mb| *mb >= 256) {
                write!(f, " · swap {:.1} GB", mb as f32 / 1024.0)?;
            }
        }
        Ok(())
    }
}

/// Swap in use right now (macOS `vm.swapusage`; ~µs, no subprocess).
#[cfg(target_os = "macos")]
pub fn swap_used_mb() -> Option<u64> {
    #[repr(C)]
    struct XswUsage {
        total: u64,
        avail: u64,
        used: u64,
        pagesize: u32,
        encrypted: bool,
    }
    let mut v = XswUsage { total: 0, avail: 0, used: 0, pagesize: 0, encrypted: false };
    let mut len = std::mem::size_of::<XswUsage>();
    let name = c"vm.swapusage";
    let rc = unsafe { libc::sysctlbyname(name.as_ptr(), (&mut v as *mut XswUsage).cast(), &mut len, std::ptr::null_mut(), 0) };
    (rc == 0).then_some(v.used / (1024 * 1024))
}

#[cfg(not(target_os = "macos"))]
pub fn swap_used_mb() -> Option<u64> {
    None
}

#[derive(Debug, Clone)]
pub struct Request {
    pub messages: Vec<Value>,
    pub max_tokens: u32,
    pub temperature: f32,
}

impl Request {
    pub fn new(messages: Vec<Value>, max_tokens: u32) -> Self {
        Self { messages, max_tokens, temperature: 0.3 }
    }
}

#[derive(Debug, Clone)]
pub struct Client {
    http: reqwest::Client,
    chat_url: String,
    model: String,
    pub deadlines: Deadlines,
}

impl Client {
    pub fn new(chat_url: impl Into<String>, model: impl Into<String>) -> Self {
        let http = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(3))
            .no_proxy()
            .build()
            .expect("HTTP client");
        Self { http, chat_url: chat_url.into(), model: model.into(), deadlines: Deadlines::default() }
    }

    pub fn model(&self) -> &str {
        &self.model
    }

    fn body(&self, req: &Request, stream: bool) -> Value {
        json!({
            "model": self.model,
            "messages": req.messages,
            "stream": stream,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "stop": ["<|endoftext|>", "<|im_end|>"],
            // Thinking off (mlx-lm defaults it ON for Gemma 4 / Qwen 3);
            // runtimes that do not know this field ignore it.
            "chat_template_kwargs": {"enable_thinking": false},
        })
    }

    /// Stream an answer.  `on_sentence` gets each sentence once, as soon as
    /// it ends.  Returns the full text and where the time went.
    pub async fn stream(
        &self,
        req: &Request,
        cancel: &CancellationToken,
        mut on_sentence: impl FnMut(&str),
    ) -> (String, Stats) {
        let t0 = Instant::now();
        let body = self.body(req, true);
        let prompt_chars = body.to_string().len();
        let mut stats = Stats {
            prompt_tokens_est: prompt_chars / 4,
            first_word: None,
            tokens: 0,
            total: Duration::ZERO,
            stop: Stop::Done,
            http_status: 0,
            error: String::new(),
        };
        let mut text = String::new();
        let mut sentences = SentenceStream::new();

        let send = self.http.post(&self.chat_url).header("Accept", "text/event-stream").json(&body).send();
        let first_deadline = t0 + self.deadlines.first_word.min(self.deadlines.total);
        let resp = tokio::select! {
            r = send => r,
            _ = cancel.cancelled() => { stats.stop = Stop::Cancelled; stats.total = t0.elapsed(); return (text, stats); }
            _ = tokio::time::sleep_until(first_deadline.into()) => {
                stats.stop = Stop::NoFirstWord; stats.total = t0.elapsed(); return (text, stats);
            }
        };
        let resp = match resp {
            Ok(r) => r,
            Err(e) => {
                stats.stop = Stop::Error;
                stats.error = e.to_string();
                stats.total = t0.elapsed();
                return (text, stats);
            }
        };
        stats.http_status = resp.status().as_u16();
        if !resp.status().is_success() {
            stats.stop = Stop::Error;
            stats.error = format!("HTTP {}", stats.http_status);
            stats.total = t0.elapsed();
            return (text, stats);
        }

        let mut bytes = resp.bytes_stream();
        let mut line_buf: Vec<u8> = Vec::new();
        'outer: loop {
            let deadline = if stats.first_word.is_none() { first_deadline } else { t0 + self.deadlines.total };
            let chunk = tokio::select! {
                c = bytes.next() => c,
                _ = cancel.cancelled() => { stats.stop = Stop::Cancelled; break; }
                _ = tokio::time::sleep_until(deadline.into()) => {
                    stats.stop = if stats.first_word.is_none() { Stop::NoFirstWord } else { Stop::TooLong };
                    break;
                }
            };
            let Some(chunk) = chunk else { break };
            let Ok(chunk) = chunk else {
                stats.stop = Stop::Error;
                stats.error = "stream broke".into();
                break;
            };
            line_buf.extend_from_slice(&chunk);
            while let Some(nl) = line_buf.iter().position(|&b| b == b'\n') {
                let line: Vec<u8> = line_buf.drain(..=nl).collect();
                let line = String::from_utf8_lossy(&line);
                let line = line.trim_end_matches(['\n', '\r']);
                let Some(payload) = line.strip_prefix("data: ") else { continue };
                if payload == "[DONE]" {
                    continue;
                }
                let Ok(j) = serde_json::from_str::<Value>(payload) else { continue };
                let token = j.pointer("/choices/0/delta/content").and_then(Value::as_str).unwrap_or("");
                if token.is_empty() {
                    continue;
                }
                if stats.first_word.is_none() {
                    stats.first_word = Some(t0.elapsed());
                }
                stats.tokens += 1;
                text.push_str(token);
                if is_looping(&text) {
                    stats.stop = Stop::Looped;
                    break 'outer;
                }
                if let Some(s) = sentences.push(token) {
                    on_sentence(&s);
                }
            }
        }
        if stats.stop != Stop::Cancelled {
            if let Some(rest) = sentences.finish() {
                on_sentence(&rest);
            }
        }
        stats.total = t0.elapsed();
        (text, stats)
    }

    /// One answer without streaming (short JSON tasks).
    pub async fn complete(&self, req: &Request, timeout: Duration) -> Result<String, String> {
        let r = self
            .http
            .post(&self.chat_url)
            .timeout(timeout)
            .json(&self.body(req, false))
            .send()
            .await
            .map_err(|e| e.to_string())?;
        if !r.status().is_success() {
            return Err(format!("HTTP {}", r.status().as_u16()));
        }
        let j: Value = r.json().await.map_err(|e| e.to_string())?;
        Ok(j.pointer("/choices/0/message/content").and_then(Value::as_str).unwrap_or("").to_string())
    }
}

#[cfg(all(test, target_os = "macos"))]
mod swap_tests {
    #[test]
    fn reads_swap() {
        let t = std::time::Instant::now();
        let mb = super::swap_used_mb();
        assert!(mb.is_some());
        assert!(t.elapsed() < std::time::Duration::from_millis(5));
        println!("swap used: {mb:?} MB in {:?}", t.elapsed());
    }
}
