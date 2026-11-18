//! The stream against a fake model server that sends SSE with real timing
//! (a raw TCP server, so tokens can arrive slowly or never).

use std::time::Duration;

use llm::{Client, Deadlines, Request, Stop};
use serde_json::json;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio_util::sync::CancellationToken;

/// Serve one request: wait `delay_first`, then send each token every `gap`.
async fn fake_model(tokens: Vec<&'static str>, delay_first: Duration, gap: Duration) -> String {
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    tokio::spawn(async move {
        let (mut s, _) = l.accept().await.unwrap();
        let mut buf = vec![0u8; 65536];
        let _ = s.read(&mut buf).await;
        let _ = s
            .write_all(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n")
            .await;
        tokio::time::sleep(delay_first).await;
        for t in tokens {
            let line = format!("data: {}\n\n", json!({"choices": [{"delta": {"content": t}}]}));
            if s.write_all(line.as_bytes()).await.is_err() {
                return;
            }
            let _ = s.flush().await;
            tokio::time::sleep(gap).await;
        }
        let _ = s.write_all(b"data: [DONE]\n\n").await;
    });
    format!("http://{addr}/v1/chat/completions")
}

fn req() -> Request {
    Request::new(vec![json!({"role": "user", "content": "hi"})], 80)
}

#[tokio::test]
async fn streams_sentences_as_they_end() {
    let url = fake_model(vec!["Paris is ", "the capital. ", "It is ", "lovely."], Duration::ZERO, Duration::from_millis(5)).await;
    let c = Client::new(url, "test");
    let mut said = vec![];
    let (text, st) = c.stream(&req(), &CancellationToken::new(), |s| said.push(s.to_string())).await;
    assert_eq!(text, "Paris is the capital. It is lovely.");
    assert_eq!(said, vec!["Paris is the capital.", "It is lovely."]);
    assert_eq!(st.stop, Stop::Done);
    assert_eq!(st.tokens, 4);
    assert!(st.first_word.is_some());
    assert!(st.to_string().starts_with("[LLM] prompt ≈"));
}

#[tokio::test]
async fn gives_up_without_a_first_word() {
    let url = fake_model(vec!["late"], Duration::from_secs(5), Duration::ZERO).await;
    let mut c = Client::new(url, "test");
    c.deadlines = Deadlines { first_word: Duration::from_millis(200), total: Duration::from_secs(2) };
    let (text, st) = c.stream(&req(), &CancellationToken::new(), |_| {}).await;
    assert_eq!(st.stop, Stop::NoFirstWord);
    assert!(text.is_empty());
    assert!(st.total < Duration::from_secs(1));
}

#[tokio::test]
async fn gives_up_when_too_long() {
    let toks: Vec<&'static str> = std::iter::repeat("word ").take(200).collect();
    let url = fake_model(toks, Duration::ZERO, Duration::from_millis(20)).await;
    let mut c = Client::new(url, "test");
    c.deadlines = Deadlines { first_word: Duration::from_secs(1), total: Duration::from_millis(300) };
    let (_, st) = c.stream(&req(), &CancellationToken::new(), |_| {}).await;
    assert_eq!(st.stop, Stop::TooLong);
    assert!(st.total < Duration::from_millis(600));
}

#[tokio::test]
async fn cuts_a_repetition_loop() {
    let toks: Vec<&'static str> = std::iter::repeat("Still working on your slides. ").take(40).collect();
    let url = fake_model(toks, Duration::ZERO, Duration::from_millis(1)).await;
    let c = Client::new(url, "test");
    let mut said = vec![];
    let (_, st) = c.stream(&req(), &CancellationToken::new(), |s| said.push(s.to_string())).await;
    assert_eq!(st.stop, Stop::Looped);
    assert_eq!(said, vec!["Still working on your slides."]);   // spoken once
    assert!(st.tokens < 40);
}

#[tokio::test]
async fn cancel_stops_immediately() {
    let toks: Vec<&'static str> = std::iter::repeat("word ").take(100).collect();
    let url = fake_model(toks, Duration::ZERO, Duration::from_millis(30)).await;
    let c = Client::new(url, "test");
    let cancel = CancellationToken::new();
    let c2 = cancel.clone();
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(150)).await;
        c2.cancel();
    });
    let mut spoken = 0;
    let (_, st) = c.stream(&req(), &cancel, |_| spoken += 1).await;
    assert_eq!(st.stop, Stop::Cancelled);
    assert!(st.total < Duration::from_millis(400));
    assert_eq!(spoken, 0);   // nothing half-said after a cancel
}

#[tokio::test]
async fn unreachable_server_is_an_error() {
    let c = Client::new("http://127.0.0.1:9/v1/chat/completions", "test");
    let (_, st) = c.stream(&req(), &CancellationToken::new(), |_| {}).await;
    assert_eq!(st.stop, Stop::Error);
}
