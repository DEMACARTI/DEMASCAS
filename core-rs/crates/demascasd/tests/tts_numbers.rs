//! Does the voice say digits right?  Kokoro renders a sentence (never played),
//! Whisper transcribes it back.  Run: cargo test --release -p demascasd --test tts_numbers -- --ignored --nocapture
use std::path::PathBuf;

fn models() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../models")
}

#[test]
#[ignore]
fn digits_are_spoken_as_numbers() {
    let tts = sherpa::Tts::kokoro(&models().join("kokoro-en-v0_19"), 2, 1.0).expect("kokoro");
    let mut rec = speech::Recognizer::load(&models().join("ggml-tiny.bin"), &models().join("ggml-base.bin"), 4).expect("whisper");
    for line in ["The speed of light is 299,792,458 meters per second.", "A leap year has 366 days.", "It costs 97 dollars and 50 cents."] {
        let pcm = tts.generate(line, 0, 1.0);
        // Kokoro's rate → Whisper's 16 kHz (linear resample)
        let ratio = tts.sample_rate as f64 / 16_000.0;
        let n = (pcm.len() as f64 / ratio) as usize;
        let pcm16: Vec<f32> = (0..n).map(|i| {
            let x = i as f64 * ratio;
            let (a, f) = (x as usize, (x - x.floor()) as f32);
            pcm[a] * (1.0 - f) + pcm.get(a + 1).copied().unwrap_or(0.0) * f
        }).collect();
        let heard = rec.command(&pcm16);
        println!("SAID:  {line}\nHEARD: {}\n", heard.text);
    }
}
