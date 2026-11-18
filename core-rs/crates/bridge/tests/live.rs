//! Round trip against a REAL tool server (read-only endpoints only).
//! Run with the server up:  cargo test -p bridge --test live -- --ignored

use bridge::Bridge;

#[tokio::test]
#[ignore = "needs the Python tool server on :5001"]
async fn live_round_trip() {
    let b = Bridge::from_env();
    let n = b.health().await.expect("tool server is not running");
    assert!(n > 50, "only {n} tools");
    assert!(b.tools().await.len() > 50);
    assert!(!b.tools_core().await.is_empty());
    let m = b.match_skill("anyways, open,").await;
    assert_eq!(m.route, "unclear");
    assert_eq!(m.ask, "Open what?");
    let chat = b.match_skill("what is the capital of France").await;
    assert_eq!(chat.route, "chat");
    let c = b.llm_config().await;
    assert!(c.context_tokens >= 1024);
    let _ = b.vocabulary().await;
    let _ = b.context("open Discord").await;
}
