//! Typed client for the DEMASCAS Python tool server (default `http://127.0.0.1:5001`).
//!
//! Replaces `daemon/src/tool_bridge.cpp`.  Same rules as the C++ bridge:
//! * every call degrades instead of failing — a dead server gives an empty
//!   result or a `[-] …` message the brain can speak, never a panic;
//! * JSON is read leniently (missing / `null` / wrong-type fields → defaults),
//!   because the server sends `"error": null` and similar.
//!
//! Endpoints are unchanged, so the Python side needs no edits.

use std::time::Duration;

use serde_json::{json, Value};

pub mod text;

pub use text::utf8_cut;

/// Default tool server address; `DEMASCAS_TOOL_SERVER` overrides it.
pub const DEFAULT_BASE: &str = "http://127.0.0.1:5001";

// ── result types ─────────────────────────────────────────────────────

/// `POST /tool`
#[derive(Debug, Clone, Default, PartialEq)]
pub struct ToolResult {
    pub result: String,
    /// Accessibility tree after a UI action (only when asked for).
    pub screen_state: String,
    /// The server reported an explicit success flag.
    pub has_success: bool,
    pub success: bool,
    /// The server looked afterwards: did it really happen?  None = not checked.
    pub verified: Option<bool>,
}

/// `POST /skill/match`
#[derive(Debug, Clone, Default, PartialEq)]
pub struct SkillMatch {
    pub matched: bool,
    /// Already handled by the server (e.g. a taught routine was saved).
    pub done: bool,
    pub skill_id: String,
    pub name: String,
    pub ack: String,
    /// A question to ask instead of acting ("Open what?").
    pub ask: String,
    pub summary: String,
    pub params: Value,
    /// No skill: "chat" | "agent" | "tool" | "unclear" | "" (brain decides).
    pub route: String,
    /// The command after the server's corrections ("cord" → "Discord").
    pub command: String,
    /// Spoken first, e.g. "Got it — Discord. I'll remember that."
    pub note: String,
    /// Web notes DEMASCAS gathered about this command.
    pub knowledge: String,
}

/// A step waiting for the user's spoken yes/no.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct PendingConfirm {
    pub token: String,
    pub speech: String,
    pub tool: String,
    /// A first-use OK on a site: after the yes, the task it paused carries on.
    pub resume: bool,
}

/// `POST /skill/run`
#[derive(Debug, Clone, Default, PartialEq)]
pub struct SkillRun {
    pub success: bool,
    pub summary: String,
    pub tool_calls: Vec<String>,
    pub confirm: Option<PendingConfirm>,
    /// A file a step made (a presentation, a document).
    pub file: String,
}

/// `POST /agent`
#[derive(Debug, Clone, Default, PartialEq)]
pub struct AgentResult {
    pub success: bool,
    /// The agent system isn't loaded or the server is unreachable.
    pub unavailable: bool,
    pub summary: String,
    pub steps_taken: i64,
    pub error: String,
    pub tool_calls: Vec<String>,
    /// Several files matched → ask which one ({name, path}, at most 5).
    pub file_choices: Vec<(String, String)>,
    pub confirm: Option<PendingConfirm>,
    pub raw: Value,
}

/// `GET /llm` — the plugged-in language model + hardware-aware thread counts.
#[derive(Debug, Clone, PartialEq)]
pub struct LlmConfig {
    pub configured: bool,
    pub runtime: String,
    pub chat_url: String,
    pub model: String,
    pub context_tokens: i64,
    pub whisper_threads: i64,
    pub tts_threads: i64,
}

impl Default for LlmConfig {
    fn default() -> Self {
        Self {
            configured: false,
            runtime: String::new(),
            chat_url: String::new(),
            model: String::new(),
            context_tokens: 4096,
            whisper_threads: 4,
            tts_threads: 4,
        }
    }
}

/// `GET /apps`
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Apps {
    pub installed: Vec<String>,
    pub running: Vec<String>,
    pub frontmost: String,
}

/// `POST /confirm`
#[derive(Debug, Clone, Default, PartialEq)]
pub struct ConfirmResult {
    pub result: String,
    pub success: bool,
}

/// `POST /learn` — one finished interaction.
#[derive(Debug, Clone, Default)]
pub struct Interaction {
    pub command: String,
    /// "shortcut" | "skill" | "chat" | "tool" | "agent" …
    pub path: String,
    pub tools: Vec<String>,
    pub result: String,
    pub success: bool,
    pub skill_id: String,
}

// ── lenient JSON readers ─────────────────────────────────────────────

/// A string field; numbers/bools are rendered, anything else is `def`.
pub fn jstr(j: &Value, key: &str, def: &str) -> String {
    match j.get(key) {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) => n.to_string(),
        Some(Value::Bool(b)) => b.to_string(),
        _ => def.to_string(),
    }
}

fn jbool(j: &Value, key: &str) -> Option<bool> {
    j.get(key).and_then(Value::as_bool)
}

fn jint(j: &Value, key: &str) -> Option<i64> {
    j.get(key).and_then(Value::as_i64)
}

fn jstrings(j: &Value, key: &str) -> Vec<String> {
    j.get(key)
        .and_then(Value::as_array)
        .map(|a| a.iter().filter_map(|v| v.as_str().map(str::to_string)).collect())
        .unwrap_or_default()
}

fn confirm_from(j: &Value) -> Option<PendingConfirm> {
    let token = jstr(j, "confirm_token", "");
    if token.is_empty() {
        return None;
    }
    Some(PendingConfirm {
        token,
        speech: jstr(j, "confirm_speech", "Should I go ahead?"),
        tool: jstr(j, "pending_tool", ""),
        resume: j.get("resume").and_then(Value::as_bool).unwrap_or(false),
    })
}

// ── the client ───────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct Bridge {
    base: String,
    http: reqwest::Client,
}

impl Default for Bridge {
    fn default() -> Self {
        Self::from_env()
    }
}

impl Bridge {
    pub fn new(base: impl Into<String>) -> Self {
        let http = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(2))
            .no_proxy() // loopback only — never route through a system proxy
            .build()
            .expect("HTTP client");
        Self { base: base.into().trim_end_matches('/').to_string(), http }
    }

    pub fn from_env() -> Self {
        Self::new(std::env::var("DEMASCAS_TOOL_SERVER").unwrap_or_else(|_| DEFAULT_BASE.into()))
    }

    pub fn base(&self) -> &str {
        &self.base
    }

    async fn get(&self, path: &str, secs: u64) -> Option<Value> {
        let r = self
            .http
            .get(format!("{}{}", self.base, path))
            .timeout(Duration::from_secs(secs))
            .send()
            .await
            .ok()?;
        r.json::<Value>().await.ok()
    }

    /// POST JSON → (parsed body, http status); `None` = no response at all.
    async fn post(&self, path: &str, body: &Value, secs: u64) -> Option<(Value, u16)> {
        let r = self
            .http
            .post(format!("{}{}", self.base, path))
            .timeout(Duration::from_secs(secs))
            .json(body)
            .send()
            .await
            .ok()?;
        let status = r.status().as_u16();
        let text = r.text().await.ok()?;
        Some((serde_json::from_str(&text).unwrap_or(Value::Null), status))
    }

    /// `GET /health` → number of tools, if the server is up.
    pub async fn health(&self) -> Option<u64> {
        let j = self.get("/health", 2).await?;
        (jstr(&j, "status", "") == "ok").then(|| j.get("tools").and_then(Value::as_u64).unwrap_or(0))
    }

    /// Wait until the server answers `/health`.
    pub async fn wait_ready(&self, tries: u32, every: Duration) -> Option<u64> {
        for i in 0..tries {
            if let Some(n) = self.health().await {
                return Some(n);
            }
            if i + 1 < tries {
                tokio::time::sleep(every).await;
            }
        }
        None
    }

    /// `GET /tools` — every tool schema (OpenAI function-calling format).
    pub async fn tools(&self) -> Vec<Value> {
        self.get("/tools", 10).await.and_then(|j| j.as_array().cloned()).unwrap_or_default()
    }

    /// `GET /tools_core` — the stable core tools (cached part of the prompt).
    pub async fn tools_core(&self) -> Vec<Value> {
        self.get("/tools_core", 5).await.and_then(|j| j.as_array().cloned()).unwrap_or_default()
    }

    /// `POST /tools_for_query` — the few tools relevant to one request.
    pub async fn tools_for_query(&self, query: &str, max_tools: usize) -> Vec<Value> {
        self.post("/tools_for_query", &json!({"query": query, "max_tools": max_tools}), 30)
            .await
            .and_then(|(j, _)| j.as_array().cloned())
            .unwrap_or_default()
    }

    /// `POST /tool` — run one tool.  `include_screen` asks for the
    /// accessibility tree after UI actions (~1–2 s, so only when needed).
    pub async fn execute(&self, name: &str, args: &Value, include_screen: bool) -> ToolResult {
        let body = json!({"name": name, "arguments": args, "include_screen": include_screen});
        let Some((j, _)) = self.post("/tool", &body, 30).await else {
            return ToolResult { result: "[-] Tool server did not respond.".into(), ..Default::default() };
        };
        let err = jstr(&j, "error", "");
        if !err.is_empty() {
            return ToolResult { result: format!("[-] {err}"), has_success: true, ..Default::default() };
        }
        let flag = jbool(&j, "success");
        ToolResult {
            result: jstr(&j, "result", "[-] No result in response."),
            screen_state: jstr(&j, "screen_state", ""),
            has_success: flag.is_some(),
            success: flag.unwrap_or(true),
            verified: jbool(&j, "verified"),
        }
    }

    /// `GET /context?query=` — learned context, pre-formatted for the prompt.
    pub async fn context(&self, query: &str) -> String {
        let q: String = url_encode(query);
        self.get(&format!("/context?query={q}"), 5)
            .await
            .map(|j| jstr(&j, "context_text", ""))
            .unwrap_or_default()
    }

    /// `POST /learn` — log a finished interaction (errors ignored).
    pub async fn learn(&self, ev: &Interaction) {
        let body = json!({
            "command": ev.command, "path": ev.path, "tools": ev.tools,
            "result": utf8_cut(&ev.result, 300), "success": ev.success, "skill_id": ev.skill_id,
        });
        let _ = self.post("/learn", &body, 5).await;
    }

    /// `POST /skill/match` — find a skill (and, if none, the route).
    pub async fn match_skill(&self, command: &str) -> SkillMatch {
        let Some((j, _)) = self.post("/skill/match", &json!({"command": command}), 20).await else {
            return SkillMatch::default();
        };
        SkillMatch {
            matched: jbool(&j, "matched").unwrap_or(false),
            done: jbool(&j, "done").unwrap_or(false),
            skill_id: jstr(&j, "skill_id", ""),
            name: jstr(&j, "name", ""),
            ack: jstr(&j, "ack", ""),
            ask: jstr(&j, "ask", ""),
            summary: jstr(&j, "summary", ""),
            params: j.get("params").filter(|p| p.is_object()).cloned().unwrap_or_else(|| json!({})),
            route: jstr(&j, "route", ""),
            command: jstr(&j, "command", ""),
            note: jstr(&j, "note", ""),
            knowledge: jstr(&j, "knowledge", ""),
        }
    }

    /// `POST /skill/run` — run a matched skill.
    pub async fn run_skill(&self, skill_id: &str, params: &Value) -> SkillRun {
        let body = json!({"skill_id": skill_id, "params": params});
        let Some((j, _)) = self.post("/skill/run", &body, 100).await else {
            return SkillRun { summary: "[-] Tool server did not respond.".into(), ..Default::default() };
        };
        SkillRun {
            success: jbool(&j, "success").unwrap_or(false),
            summary: jstr(&j, "summary", ""),
            tool_calls: jstrings(&j, "tool_calls"),
            confirm: confirm_from(&j),
            file: jstr(&j, "file", ""),
        }
    }

    /// `POST /feedback` — "good job" / "that's wrong" about the last result.
    pub async fn feedback(&self, positive: bool) {
        let _ = self.post("/feedback", &json!({"positive": positive}), 5).await;
    }

    /// `GET /vocabulary` — words the recogniser should expect.
    pub async fn vocabulary(&self) -> Vec<String> {
        self.get("/vocabulary", 5).await.map(|j| jstrings(&j, "terms")).unwrap_or_default()
    }

    /// `POST /agent` — a domain agent runs a multi-step task.  The timeout
    /// exceeds the agent's own budget so the agent always stops first.
    pub async fn agent(&self, command: &str, context: &Value) -> AgentResult {
        let ctx = if context.is_object() { context.clone() } else { json!({}) };
        let body = json!({"command": command, "context": ctx});
        let r = self
            .http
            .post(format!("{}/agent", self.base))
            .timeout(Duration::from_secs(100))
            .json(&body)
            .send()
            .await;
        let resp = match r {
            Ok(r) => r,
            Err(e) => {
                return AgentResult {
                    unavailable: e.is_connect(),
                    error: format!("HTTP error: {e}"),
                    ..Default::default()
                }
            }
        };
        let unavailable = resp.status().as_u16() == 503;
        let text = resp.text().await.unwrap_or_default();
        if text.is_empty() {
            return AgentResult { unavailable, error: "Empty response from /agent endpoint.".into(), ..Default::default() };
        }
        let Ok(j) = serde_json::from_str::<Value>(&text) else {
            return AgentResult { unavailable, error: "JSON parse error".into(), ..Default::default() };
        };
        let mut file_choices = Vec::new();
        if let Some(arr) = j.pointer("/data/pending_file_choices").and_then(Value::as_array) {
            for fc in arr {
                let path = jstr(fc, "path", "");
                if path.is_empty() {
                    continue;
                }
                file_choices.push((jstr(fc, "name", &path), path));
                if file_choices.len() >= 5 {
                    break;
                }
            }
        }
        AgentResult {
            success: jbool(&j, "success").unwrap_or(false),
            unavailable,
            summary: jstr(&j, "summary", ""),
            steps_taken: jint(&j, "steps_taken").unwrap_or(0),
            error: jstr(&j, "error", ""),
            tool_calls: jstrings(&j, "tool_calls"),
            file_choices,
            confirm: j.get("pending_confirmation").filter(|p| p.is_object()).and_then(confirm_from),
            raw: j,
        }
    }

    /// `POST /agent/cancel` — stop the running agent before its next step.
    pub async fn agent_cancel(&self) {
        let _ = self.post("/agent/cancel", &json!({}), 3).await;
    }

    /// `GET /agent/status` — {"running", "goal", "step", "last_tool", "elapsed"}.
    pub async fn agent_status(&self) -> Value {
        self.get("/agent/status", 5).await.filter(Value::is_object).unwrap_or_else(|| json!({}))
    }

    /// `GET /screen_context` — structured screen digest for the prompt.
    pub async fn screen_context(&self, force: bool) -> String {
        let path = if force { "/screen_context?force=true" } else { "/screen_context" };
        self.get(path, 5).await.map(|j| jstr(&j, "context_block", "")).unwrap_or_default()
    }

    /// `GET /session` — cross-turn active context (open app, folder, file …).
    pub async fn session(&self) -> Value {
        self.get("/session", 5).await.filter(Value::is_object).unwrap_or_else(|| json!({}))
    }

    /// `GET /preferences` — the user's rules, pre-formatted for the prompt.
    pub async fn preferences(&self) -> String {
        self.get("/preferences", 5).await.map(|j| jstr(&j, "preferences", "")).unwrap_or_default()
    }

    /// `GET /apps` — installed + running apps and the app in front.
    pub async fn apps(&self, with_frontmost: bool) -> Apps {
        let path = if with_frontmost { "/apps" } else { "/apps?frontmost=0" };
        self.get(path, 5)
            .await
            .map(|j| Apps { installed: jstrings(&j, "installed"), running: jstrings(&j, "running"), frontmost: jstr(&j, "frontmost", "") })
            .unwrap_or_default()
    }

    /// `GET /brief/status` — has today's morning brief been given?
    pub async fn briefed_today(&self) -> bool {
        self.get("/brief/status", 3).await.and_then(|j| jbool(&j, "briefed_today")).unwrap_or(true)
    }

    /// `GET /channel/inbox` — texts from the owner (iMessage): [(text, from)], and
    /// whether the channel is switched on.
    pub async fn channel_inbox(&self) -> (Vec<(String, String)>, bool) {
        let Some(j) = self.get("/channel/inbox", 5).await else { return (Vec::new(), false) };
        let msgs = j.get("messages").and_then(Value::as_array).map(|a| a.iter().map(|m| (jstr(m, "text", ""), jstr(m, "from", ""))).filter(|(t, _)| !t.is_empty()).collect()).unwrap_or_default();
        let on = j.pointer("/status/enabled").and_then(Value::as_bool).unwrap_or(false);
        (msgs, on)
    }

    /// `POST /channel/reply` — answer the owner over iMessage ("" = the owner).
    pub async fn channel_reply(&self, to: &str, text: &str) -> bool {
        self.post("/channel/reply", &json!({"to": to, "text": text}), 20).await.and_then(|(j, _)| jbool(&j, "sent")).unwrap_or(false)
    }

    /// `POST /pending` — hold a tool call until the user says yes.
    pub async fn pending(&self, name: &str, args: &Value, speech: &str) -> Option<PendingConfirm> {
        let (j, _) = self.post("/pending", &json!({"name": name, "arguments": args, "speech": speech}), 5).await?;
        confirm_from(&j)
    }

    /// `GET /ui?app=` — the app's on-screen controls ("[button] Friends").
    pub async fn ui(&self, app: &str, limit: usize) -> Vec<String> {
        self.get(&format!("/ui?app={}&limit={limit}", url_encode(app)), 5).await.map(|j| jstrings(&j, "elements")).unwrap_or_default()
    }

    /// `GET /app_hints` — what DEMASCAS learned about an app for this goal.
    /// An app it never studied starts being learned in the background.
    pub async fn app_hints(&self, app: &str, goal: &str) -> (bool, String) {
        self.get(&format!("/app_hints?app={}&goal={}", url_encode(app), url_encode(goal)), 5)
            .await
            .map(|j| (jbool(&j, "known").unwrap_or(false), jstr(&j, "hints", "")))
            .unwrap_or_default()
    }

    /// `GET /llm` — the plugged-in model and thread counts (read at startup).
    pub async fn llm_config(&self) -> LlmConfig {
        let Some(j) = self.get("/llm", 5).await else {
            return LlmConfig::default();
        };
        let d = LlmConfig::default();
        LlmConfig {
            configured: jbool(&j, "configured").unwrap_or(false),
            runtime: jstr(&j, "runtime", ""),
            chat_url: jstr(&j, "chat_url", ""),
            model: jstr(&j, "model", ""),
            context_tokens: jint(&j, "context_tokens").unwrap_or(d.context_tokens).max(1024),
            whisper_threads: jint(&j, "whisper_threads").unwrap_or(d.whisper_threads).max(1),
            tts_threads: jint(&j, "tts_threads").unwrap_or(d.tts_threads).max(1),
        }
    }

    /// `POST /confirm` — approve or reject a pending action.
    pub async fn confirm(&self, token: &str, approve: bool) -> ConfirmResult {
        match self.post("/confirm", &json!({"token": token, "approve": approve}), 30).await {
            Some((j, _)) => ConfirmResult {
                result: jstr(&j, "result", "[-] No result in response."),
                success: jbool(&j, "success").unwrap_or(false),
            },
            None => ConfirmResult { result: "[-] Tool server did not respond.".into(), success: false },
        }
    }
}

/// Percent-encode a query component (RFC 3986 unreserved kept).
pub fn url_encode(s: &str) -> String {
    let mut out = String::with_capacity(s.len() * 3);
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => out.push(b as char),
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lenient_strings() {
        let j = json!({"a": "x", "b": null, "c": 3, "d": true, "e": [1]});
        assert_eq!(jstr(&j, "a", "-"), "x");
        assert_eq!(jstr(&j, "b", "-"), "-");
        assert_eq!(jstr(&j, "c", "-"), "3");
        assert_eq!(jstr(&j, "d", "-"), "true");
        assert_eq!(jstr(&j, "e", "-"), "-");
        assert_eq!(jstr(&j, "missing", "-"), "-");
    }

    #[test]
    fn encodes_queries() {
        assert_eq!(url_encode("open Discord"), "open%20Discord");
        assert_eq!(url_encode("a&b=c"), "a%26b%3Dc");
        assert_eq!(url_encode("café"), "caf%C3%A9");
    }
}
