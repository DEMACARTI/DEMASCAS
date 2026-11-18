//! demascasd — the DEMASCAS daemon (Rust).
//!
//!   DEMASCAS_TEXT_MODE=1 demascasd   typed lines in, spoken lines printed (the exam)
//!   demascasd                        voice: microphone + Kokoro (see `voice` mode)
//!
//! Talks to the Python tool server (DEMASCAS_TOOL_SERVER, default :5001)
//! and to whatever local model the user plugged in (from GET /llm).

use std::sync::Arc;
use std::time::Duration;

use bridge::Bridge;
use brain::engine::Engine;

mod control;
mod io;
mod mem;
mod session;
mod voiceio;

use io::{TextEars, TextMouth};
use session::{Config, Session};

fn text_mode() -> bool {
    std::env::var("DEMASCAS_TEXT_MODE").is_ok_and(|v| !v.is_empty() && v != "0")
}

#[tokio::main(flavor = "multi_thread", worker_threads = 4)]
async fn main() {
    println!("DEMASCAS — Rust daemon {}", env!("CARGO_PKG_VERSION"));
    let bridge = Bridge::from_env();
    let Some(n) = bridge.wait_ready(60, Duration::from_millis(500)).await else {
        eprintln!("[FATAL] Tool server not reachable at {} — start it first (python main.py).", bridge.base());
        std::process::exit(1);
    };
    println!("[✓] Tool server ready ({n} tools)");
    // replies slow down 5–10× when the Mac swaps the model out (18 Nov log)
    let swap = llm::swap_used_mb().unwrap_or(0);
    println!("[MEM] swap in use {:.1} GB", swap as f32 / 1024.0);
    mem::start_watch();

    let cfg = bridge.llm_config().await;
    let llm = if cfg.configured && !cfg.chat_url.is_empty() {
        println!("[✓] Model: {} via {} ({})", cfg.model, cfg.runtime, cfg.chat_url);
        Some(llm::Client::new(cfg.chat_url.clone(), cfg.model.clone()))
    } else {
        eprintln!("[!] No language model is set up yet — only shortcuts and direct actions will work. Run: python main.py setup");
        None
    };
    let ctx = cfg.context_tokens.max(1024) as usize;
    let mut main_engine = Engine::new(bridge.clone(), llm.clone(), ctx);
    let mut worker = Engine::new(bridge.clone(), llm, ctx);
    let core = main_engine.load_core_tools().await;
    worker.load_core_tools().await;
    println!("[✓] {core} core tools in the cached prompt");
    {
        // prime the model server's prompt cache without delaying startup
        let warm = Engine::new(bridge.clone(), main_engine.llm.clone(), ctx);
        tokio::spawn(async move {
            let mut warm = warm;
            warm.load_core_tools().await;
            let t = std::time::Instant::now();
            let parts = warm.prewarm().await;
            println!("[✓] Model warm-up done ({:.1} s: {parts})", t.elapsed().as_secs_f32());
        });
    }

    if text_mode() {
        let session = Session::new(
            TextEars::new(),
            Arc::new(TextMouth::default()),
            bridge,
            main_engine,
            worker,
            Config { followup: Duration::from_secs(8), wake_ack: None, wait_for_tasks: true },
            Arc::new(events::Schedule::load_default()),
        );
        session.run().await;
        return;
    }

    // ── voice mode
    let models = models_dir();
    println!("[*] Models: {}", models.display());
    let speaker = match audio::Speaker::open() {
        Ok(s) => Arc::new(s),
        Err(e) => fatal(&format!("Cannot open the speaker: {e}")),
    };
    let mic = match audio::Mic::open() {
        Ok(m) => m,
        Err(e) => fatal(&format!("Cannot open the microphone: {e}. Check System Settings → Privacy & Security → Microphone.")),
    };
    println!("[✓] Microphone {} · speaker {} Hz", mic.device_name, speaker.rate);
    let aec = Arc::new(audio::EchoCanceller::standard().unwrap_or_else(|| fatal("echo canceller (speexdsp) failed to start")));
    speaker.set_echo_canceller(aec.clone());
    let voice = voice::Voice::new(speaker.clone(), &models.join("kokoro-en-v0_19"), cfg.tts_threads as i32);
    println!("[✓] Voice: {}", voice.backend);
    let vad_model = models.join("silero_vad.onnx");
    let (vad, barge_vad) = match (sherpa::Vad::standard(&vad_model), sherpa::Vad::standard(&vad_model)) {
        (Ok(a), Ok(b)) => (a, b),
        (Err(e), _) | (_, Err(e)) => fatal(&format!("speech detector: {e} — run ./scripts/build.sh")),
    };
    let mut recog = speech::Recognizer::load(&models.join("ggml-tiny.bin"), &models.join("ggml-base.bin"), cfg.whisper_threads as i32)
        .unwrap_or_else(|e| fatal(&format!("{e} — run ./scripts/build.sh (downloads the speech models)")));
    let mut prompt = WHISPER_PROMPT.to_string();
    for t in bridge.vocabulary().await.into_iter().take(40) {
        if prompt.len() + t.len() > 700 {
            break;
        }
        if !prompt.contains(&t) {
            prompt.push_str(", ");
            prompt.push_str(&t);
        }
    }
    recog.set_prompt(&prompt);
    let t = std::time::Instant::now();
    tokio::task::block_in_place(|| recog.warm_up());
    println!("[✓] Recogniser ready ({:.1} s warm-up)", t.elapsed().as_secs_f32());

    let mouth = Arc::new(voiceio::VoiceMouth { voice });
    let ears = voiceio::VoiceEars::new(mic, aec, vad, barge_vad, recog, mouth.clone());
    println!("\n[*] DEMASCAS is ready.  Say \"DEMASCAS\" to start.\n");
    let session = Session::new(
        ears,
        mouth,
        bridge,
        main_engine,
        worker,
        Config { followup: Duration::from_secs(8), wake_ack: Some("At your service.".into()), wait_for_tasks: false },
        Arc::new(events::Schedule::load_default()),
    );
    let mut session = session;
    session.enable_channel(); // texts from your phone (iMessage, owner-only)
    session.run().await;
}

/// Biases the recogniser towards words DEMASCAS expects (names, apps, commands).
const WHISPER_PROMPT: &str = "DEMASCAS, Daksh, open, close, search, remember, send a message, screenshot, Safari, Chrome, YouTube, minimize, maximize, copy, paste, undo, redo, quit, scroll, what, how, why, my name is, stop, bye, goodbye, thank you";

fn fatal(msg: &str) -> ! {
    eprintln!("[FATAL] {msg}");
    std::process::exit(1);
}

/// `DEMASCAS_MODELS`, else `models` here, else next to the build.
fn models_dir() -> std::path::PathBuf {
    if let Ok(d) = std::env::var("DEMASCAS_MODELS") {
        return d.into();
    }
    let here = std::path::PathBuf::from("models");
    if here.join("ggml-base.bin").exists() {
        return here;
    }
    let exe = std::env::current_exe().unwrap_or_default();
    for up in exe.ancestors().skip(1).take(5) {
        let c = up.join("models");
        if c.join("ggml-base.bin").exists() {
            return c;
        }
        let c = up.join("../models");
        if c.join("ggml-base.bin").exists() {
            return c;
        }
    }
    here
}
