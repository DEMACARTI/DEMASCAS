//! Print the router's decision for each request on stdin (one per line,
//! optional "~0.4|" confidence prefix), using the real tool server's
//! /skill/match verdict.  Used to check routing against the exam:
//!   ../venv/bin/python scripts/exam_routes.py
use std::io::BufRead;

use brain::reflex::chrono_lite::Now;
use brain::router::{decide, Context};

#[tokio::main]
async fn main() {
    let b = bridge::Bridge::from_env();
    for line in std::io::stdin().lock().lines().map_while(Result::ok) {
        let (conf, text) = match line.strip_prefix('~').and_then(|r| r.split_once('|')) {
            Some((c, t)) => (c.parse().unwrap_or(1.0), t.to_string()),
            None => (1.0, line.clone()),
        };
        let m = b.match_skill(&text).await;
        let ctx = Context { first_after_wake: true, confidence: conf, unsure: if conf < 0.5 { 0.4 } else { 0.0 }, ..Default::default() };
        let d = decide(&text, &ctx, &m, Now::local());
        println!("{}\t{}\t{:?}", text, m.route, d);
    }
}
