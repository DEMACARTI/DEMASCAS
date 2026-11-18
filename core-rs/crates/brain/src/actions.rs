//! Reading the model's output and the tools' results (ported from
//! `parse_llm_action`, `extract_json_objects`, `is_tool_error`).

use serde_json::{json, Value};

#[derive(Debug, Clone, PartialEq)]
pub enum Action {
    Call { tool: String, args: Value },
    Done { say: String },
}

/// Top-level `{…}` slices in free text (recovers "{a},{b}", prose + JSON).
pub fn json_objects(text: &str) -> Vec<&str> {
    let (mut out, mut depth, mut start, mut in_str, mut esc) = (Vec::new(), 0usize, None, false, false);
    for (i, c) in text.char_indices() {
        if in_str {
            if esc {
                esc = false;
            } else if c == '\\' {
                esc = true;
            } else if c == '"' {
                in_str = false;
            }
            continue;
        }
        match c {
            '"' => in_str = true,
            '{' => {
                if depth == 0 {
                    start = Some(i);
                }
                depth += 1;
            }
            '}' if depth > 0 => {
                depth -= 1;
                if depth == 0 {
                    if let Some(s) = start.take() {
                        out.push(&text[s..=i]);
                    }
                }
            }
            _ => {}
        }
    }
    out
}

const FINISH: &[&str] = &["done", "finish", "final_answer", "answer", "respond", "reply_to_user", "say"];

/// The first tool call or final answer in the model's output; plain text
/// with no JSON is treated as the spoken answer.
pub fn parse_action(raw: &str) -> Action {
    for obj in json_objects(raw) {
        let Ok(j) = serde_json::from_str::<Value>(obj) else { continue };
        if let Some(tool) = j.get("tool").and_then(Value::as_str) {
            let args = j.get("args").filter(|a| a.is_object()).cloned().unwrap_or_else(|| json!({}));
            if FINISH.contains(&tool) {
                // small models write the answer as a "done" TOOL call
                let say = ["say", "text", "message"].iter().find_map(|k| args.get(*k).and_then(Value::as_str)).unwrap_or("");
                return Action::Done { say: say.to_string() };
            }
            return Action::Call { tool: tool.to_string(), args };
        }
        if j.get("done").and_then(Value::as_bool) == Some(true) {
            return Action::Done { say: j.get("say").and_then(Value::as_str).unwrap_or("").to_string() };
        }
    }
    Action::Done { say: raw.trim().to_string() }
}

/// Did the tool fail?  Only the START of the text counts — page text and
/// file lists often contain "not found" without being failures.
pub fn is_tool_error(result: &str) -> bool {
    let t = result.trim_start();
    if t.is_empty() {
        return true;
    }
    let head: String = t.chars().take(24).collect::<String>().to_lowercase();
    if ["[-]", "failed", "error", "timeout", "could not", "refused"].iter().any(|p| head.starts_with(p)) {
        return true;
    }
    if t.starts_with('{') {
        if let Ok(j) = serde_json::from_str::<Value>(t) {
            if j.get("success").and_then(Value::as_bool) == Some(false) {
                return true;
            }
            if j.get("message").and_then(Value::as_str).is_some_and(|m| m.starts_with("[-]")) {
                return true;
            }
        }
    }
    false
}

/// A tool result that is a pending confirmation (the server holds the action).
pub fn pending_confirmation(result: &str) -> Option<bridge::PendingConfirm> {
    if !result.contains("pending_confirmation") {
        return None;
    }
    let j: Value = serde_json::from_str(result.trim()).ok()?;
    if j.get("status").and_then(Value::as_str) != Some("pending_confirmation") {
        return None;
    }
    let token = bridge::jstr(&j, "confirm_token", "");
    (!token.is_empty()).then(|| bridge::PendingConfirm {
        token,
        speech: bridge::jstr(&j, "confirm_speech", "Should I go ahead?"),
        tool: bridge::jstr(&j, "pending_tool", ""),
        resume: j.get("resume").and_then(Value::as_bool).unwrap_or(false),
    })
}

/// The speakable part of a tool result ({"message": …} or the text itself).
pub fn speakable(result: &str) -> String {
    let t = result.trim();
    if t.starts_with('{') {
        if let Ok(j) = serde_json::from_str::<Value>(t) {
            for k in ["message", "summary", "result", "say"] {
                if let Some(s) = j.get(k).and_then(Value::as_str) {
                    return s.trim_start_matches("[-]").trim().to_string();
                }
            }
        }
    }
    t.trim_start_matches("[-]").trim_start_matches("Success:").trim().to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_calls_and_answers() {
        assert_eq!(parse_action(r#"{"tool":"open_application","args":{"app_name":"Discord"}}"#),
                   Action::Call { tool: "open_application".into(), args: json!({"app_name": "Discord"}) });
        assert_eq!(parse_action(r#"Sure! {"done":true,"say":"Paris."}"#), Action::Done { say: "Paris.".into() });
        assert_eq!(parse_action(r#"{"tool":"done","args":{"say":"All set."}}"#), Action::Done { say: "All set.".into() });
        assert_eq!(parse_action("Just text."), Action::Done { say: "Just text.".into() });
        // two calls in one reply → the first
        assert!(matches!(parse_action(r#"{"tool":"a","args":{}},{"tool":"b","args":{}}"#), Action::Call { tool, .. } if tool == "a"));
        // braces inside strings don't confuse it
        assert!(matches!(parse_action(r#"{"tool":"type_text","args":{"text":"a } b {"}}"#), Action::Call { .. }));
    }

    #[test]
    fn tool_errors() {
        assert!(is_tool_error("[-] Unknown tool"));
        assert!(is_tool_error(r#"{"success": false, "message": "x"}"#));
        assert!(!is_tool_error("Success: Opened 'Discord'."));
        assert!(!is_tool_error("Page text: the file was not found in 1999"));
    }

    #[test]
    fn confirmations_and_speech() {
        let r = r#"{"status": "pending_confirmation", "confirm_speech": "Send 'hi' to Hatim on Discord?", "pending_tool": "deliver_chat_message", "confirm_token": "t"}"#;
        assert_eq!(pending_confirmation(r).unwrap().speech, "Send 'hi' to Hatim on Discord?");
        assert!(pending_confirmation("Success").is_none());
        assert_eq!(speakable(r#"{"success": true, "message": "Found 2 files."}"#), "Found 2 files.");
        assert_eq!(speakable("Success: Opened 'Discord'."), "Opened 'Discord'.");
    }
}
