//! Each endpoint against a mock server with the shapes the Python server
//! really sends (nulls included), plus a dead server.

use bridge::{Bridge, Interaction};
use serde_json::json;
use wiremock::matchers::{body_partial_json, method, path, query_param};
use wiremock::{Mock, MockServer, ResponseTemplate};

async fn server() -> (MockServer, Bridge) {
    let s = MockServer::start().await;
    let b = Bridge::new(s.uri());
    (s, b)
}

fn ok(body: serde_json::Value) -> ResponseTemplate {
    ResponseTemplate::new(200).set_body_json(body)
}

#[tokio::test]
async fn health_and_tools() {
    let (s, b) = server().await;
    Mock::given(method("GET")).and(path("/health")).respond_with(ok(json!({"status": "ok", "tools": 114}))).mount(&s).await;
    Mock::given(method("GET")).and(path("/tools_core"))
        .respond_with(ok(json!([{"type": "function", "function": {"name": "open_application"}}]))).mount(&s).await;
    assert_eq!(b.health().await, Some(114));
    assert_eq!(b.tools_core().await.len(), 1);
}

#[tokio::test]
async fn execute_reads_success_and_screen() {
    let (s, b) = server().await;
    Mock::given(method("POST")).and(path("/tool"))
        .and(body_partial_json(json!({"name": "open_application", "arguments": {"app_name": "Discord"}})))
        .respond_with(ok(json!({"result": "Success: Opened 'Discord'.", "success": true, "screen_state": "AXWindow"})))
        .mount(&s).await;
    let r = b.execute("open_application", &json!({"app_name": "Discord"}), true).await;
    assert_eq!(r.result, "Success: Opened 'Discord'.");
    assert!(r.has_success && r.success);
    assert_eq!(r.screen_state, "AXWindow");
}

#[tokio::test]
async fn execute_failure_and_missing_result() {
    let (s, b) = server().await;
    Mock::given(method("POST")).and(path("/tool"))
        .respond_with(ok(json!({"success": false, "result": null}))).mount(&s).await;
    let r = b.execute("x", &json!({}), false).await;
    assert!(r.has_success && !r.success);
    assert_eq!(r.result, "[-] No result in response.");
}

#[tokio::test]
async fn skill_match_route_and_nulls() {
    let (s, b) = server().await;
    Mock::given(method("POST")).and(path("/skill/match")).respond_with(ok(json!({
        "matched": false, "route": "unclear", "command": "anyways, open,", "note": null,
        "knowledge": "", "ask": "Open what?"
    }))).mount(&s).await;
    let m = b.match_skill("anyways, open,").await;
    assert!(!m.matched);
    assert_eq!(m.route, "unclear");
    assert_eq!(m.ask, "Open what?");
    assert_eq!(m.note, "");
    assert_eq!(m.params, json!({}));
}

#[tokio::test]
async fn skill_run_with_confirmation() {
    let (s, b) = server().await;
    Mock::given(method("POST")).and(path("/skill/run")).respond_with(ok(json!({
        "success": true, "summary": "Typed it.", "tool_calls": ["send_chat_message"],
        "confirm_token": "tok", "confirm_speech": "Send 'hi' to Hatim on Discord?",
        "pending_tool": "deliver_chat_message"
    }))).mount(&s).await;
    let r = b.run_skill("message_contact", &json!({"request": "send hi to Hatim"})).await;
    let c = r.confirm.expect("pending confirmation");
    assert_eq!(c.token, "tok");
    assert_eq!(c.tool, "deliver_chat_message");
    assert_eq!(r.tool_calls, vec!["send_chat_message"]);
}

#[tokio::test]
async fn agent_file_choices_and_confirm() {
    let (s, b) = server().await;
    Mock::given(method("POST")).and(path("/agent")).respond_with(ok(json!({
        "success": true, "summary": "Which one?", "steps_taken": 2, "error": null,
        "tool_calls": ["find_file"],
        "data": {"pending_file_choices": [
            {"name": "a.pdf", "path": "/x/a.pdf"}, {"path": "/x/b.pdf"}, {"name": "no path"}]},
        "pending_confirmation": {"confirm_token": "t2", "pending_tool": "delete_file"}
    }))).mount(&s).await;
    let r = b.agent("open my report", &json!(null)).await;
    assert!(r.success);
    assert_eq!(r.error, "");
    assert_eq!(r.steps_taken, 2);
    assert_eq!(r.file_choices, vec![("a.pdf".into(), "/x/a.pdf".into()), ("/x/b.pdf".into(), "/x/b.pdf".into())]);
    let c = r.confirm.unwrap();
    assert_eq!(c.speech, "Should I go ahead?");
}

#[tokio::test]
async fn agent_unavailable_on_503() {
    let (s, b) = server().await;
    Mock::given(method("POST")).and(path("/agent"))
        .respond_with(ResponseTemplate::new(503).set_body_json(json!({"success": false, "error": "agents not loaded"})))
        .mount(&s).await;
    let r = b.agent("x", &json!({})).await;
    assert!(r.unavailable && !r.success);
}

#[tokio::test]
async fn llm_config_clamps() {
    let (s, b) = server().await;
    Mock::given(method("GET")).and(path("/llm")).respond_with(ok(json!({
        "configured": true, "runtime": "mlx", "chat_url": "http://127.0.0.1:8081/v1/chat/completions",
        "model": "qwen", "context_tokens": 512, "whisper_threads": 0
    }))).mount(&s).await;
    let c = b.llm_config().await;
    assert!(c.configured);
    assert_eq!(c.context_tokens, 1024);
    assert_eq!(c.whisper_threads, 1);
    assert_eq!(c.tts_threads, 4);
}

#[tokio::test]
async fn context_query_is_encoded() {
    let (s, b) = server().await;
    Mock::given(method("GET")).and(path("/context")).and(query_param("query", "open Discord & chat"))
        .respond_with(ok(json!({"context_text": "user likes Discord", "profile_summary": ""}))).mount(&s).await;
    assert_eq!(b.context("open Discord & chat").await, "user likes Discord");
}

#[tokio::test]
async fn confirm_vocabulary_learn() {
    let (s, b) = server().await;
    Mock::given(method("POST")).and(path("/confirm")).and(body_partial_json(json!({"token": "t", "approve": true})))
        .respond_with(ok(json!({"result": "Sent.", "success": true}))).mount(&s).await;
    Mock::given(method("GET")).and(path("/vocabulary"))
        .respond_with(ok(json!({"terms": ["Discord", 3, "DEMASCAS"]}))).mount(&s).await;
    Mock::given(method("POST")).and(path("/learn")).respond_with(ok(json!({"status": "queued"}))).expect(1).mount(&s).await;
    assert_eq!(b.confirm("t", true).await.result, "Sent.");
    assert_eq!(b.vocabulary().await, vec!["Discord", "DEMASCAS"]);
    b.learn(&Interaction { command: "open Discord".into(), result: "✓".repeat(200), ..Default::default() }).await;
}

#[tokio::test]
async fn dead_server_degrades() {
    let b = Bridge::new("http://127.0.0.1:9"); // nothing listens on the discard port
    assert_eq!(b.health().await, None);
    assert_eq!(b.execute("x", &json!({}), false).await.result, "[-] Tool server did not respond.");
    assert!(!b.match_skill("hi").await.matched);
    assert!(b.agent("x", &json!({})).await.unavailable);
    assert_eq!(b.llm_config().await.context_tokens, 4096);
    assert!(!b.confirm("t", true).await.success);
    assert!(b.tools().await.is_empty());
}
