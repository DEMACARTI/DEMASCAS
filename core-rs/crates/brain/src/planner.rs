//! The planner — think before a multi-step task.
//!
//! The old `architect_plan` tool waited for the model to decide to plan
//! (a 3B model never did).  Here the BRAIN decides: a request with several
//! actions ("find my report, summarise it and email it to Aman") gets one
//! planning call that returns structured steps; each step then runs through
//! a reflex / single tool / the screen-grounded loop, is checked, and a
//! failure triggers one re-plan.  The user hears the plan in one line first.

use serde_json::Value;

use crate::actions::json_objects;
use crate::utterance::simplify;

#[derive(Debug, Clone, PartialEq)]
pub struct Step {
    /// Short imperative: "find the project report".
    pub goal: String,
    /// A tool that does the whole step in one call (optional).
    pub tool: Option<String>,
    pub args: Value,
    /// How to know it worked.
    pub check: String,
}

const ACTION_VERBS: &[&str] = &[
    "find", "open", "summarise", "summarize", "send", "email", "mail", "draft", "create", "make", "research", "write",
    "compare", "copy", "save", "move", "rename", "organise", "organize", "prepare", "schedule", "read", "check",
    "search", "download", "upload", "share", "post", "reply", "add", "delete", "play", "go", "click", "type", "close",
    "message", "text", "remind", "book", "list", "collect", "gather", "translate", "set",
];

/// Does this request need a plan?  (several actions, or a goal-shaped request)
pub fn needs_plan(text: &str) -> bool {
    let s = simplify(text);
    let words: Vec<&str> = s.split_whitespace().collect();
    let goal_start = ["research", "prepare", "organise", "organize", "plan", "compare", "set up", "collect", "gather", "figure out", "get me ready"]
        .iter().any(|g| s.starts_with(g));
    if goal_start && words.len() >= 3 {
        return true;
    }
    if words.len() < 5 {
        return false;
    }
    let verbs = words.iter().filter(|w| ACTION_VERBS.contains(w)).count();
    let joined = s.contains(" and ") || s.contains(" then ") || text.contains(',');
    let goal_start = ["research", "prepare", "organise", "organize", "plan", "compare", "set up", "collect", "gather", "figure out"]
        .iter().any(|g| s.starts_with(g));
    (verbs >= 2 && joined && words.len() >= 7) || (goal_start && words.len() >= 4) || crate::reflex::split_compound(text).len() >= 3
}

pub const PLAN_PROMPT: &str = "You plan tasks for DEMASCAS, an assistant that controls a Mac.
Split the REQUEST into 2 to 5 steps, in order.
Prefer ONE powerful tool that does a whole job over many small screen steps:
  research_web = reads the web about a topic and answers
  list_agenda, read_mail, read_chat_messages, list_reminders, find_contact, read_notes = what's going on
  create_presentation / create_document = ONLY when the request asks for slides or a document
  find_file, open_file, summarize_latest_pdf
  compose_email = drafts an email (never sends)
  message_contact = writes a chat message and asks before sending
  read_chat_messages = reads someone's recent messages
Write each step on its own line exactly as:  N. tool_name | what to do
Write - instead of a tool name when the step needs looking at the screen.
Do only what was asked.

End with a step '- | tell me ...' when the user wants to know something.

Example
REQUEST: find my budget sheet and email it to Aman
1. find_file | find the budget spreadsheet
2. compose_email | draft an email to Aman about the budget spreadsheet

Example
REQUEST: prepare for my meeting with Aman tomorrow
1. list_agenda | check tomorrow's meeting with Aman
2. read_mail | look for recent mail from Aman
3. read_chat_messages | check recent messages from Aman
4. - | tell me what I should know for the meeting

Other tools you may name: ";

/// The planning SYSTEM prompt — identical every time (so the model server
/// keeps it cached); what's happening right now goes in the user message.
pub fn plan_prompt(tools_text: &str) -> String {
    let names: Vec<&str> = tools_text.lines().filter_map(|l| l.trim_start_matches("- ").split('(').next()).filter(|n| !n.is_empty()).collect();
    format!("{PLAN_PROMPT}{}\n", names.join(", "))
}

/// "1. find_file | find the report" lines → steps (JSON plans still accepted).
pub fn parse_plan(raw: &str) -> Vec<Step> {
    let mut steps = Vec::new();
    for line in raw.lines() {
        let l = line.trim().trim_start_matches(['-', '*', ' ']);
        let Some((num, rest)) = l.split_once(['.', ')']) else { continue };
        if num.trim().parse::<u32>().is_err() {
            continue;
        }
        let (tool, goal) = match rest.split_once('|') {
            Some((t, g)) => (t.trim(), g.trim()),
            None => ("-", rest.trim()),
        };
        if goal.is_empty() {
            continue;
        }
        let tool = tool.trim_matches(|c: char| c == '`' || c == '"' || c == ' ');
        let tool = (!tool.is_empty() && tool != "-" && tool != "none" && tool.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')).then(|| tool.to_string());
        steps.push(Step { goal: goal.to_string(), tool, args: serde_json::json!({}), check: String::new() });
        if steps.len() == 6 {
            break;
        }
    }
    if !steps.is_empty() {
        return steps;
    }
    // older JSON form
    for obj in json_objects(raw) {
        let Ok(j) = serde_json::from_str::<Value>(obj) else { continue };
        let Some(arr) = j.get("steps").and_then(Value::as_array) else { continue };
        let steps: Vec<Step> = arr.iter().filter_map(|st| {
            let goal = st.get("goal").and_then(Value::as_str).unwrap_or("").trim().to_string();
            if goal.is_empty() {
                return None;
            }
            let tool = st.get("tool").and_then(Value::as_str).map(str::trim).filter(|t| !t.is_empty() && *t != "none").map(str::to_string);
            Some(Step {
                goal,
                tool,
                args: st.get("args").filter(|a| a.is_object()).cloned().unwrap_or_else(|| serde_json::json!({})),
                check: st.get("check").and_then(Value::as_str).unwrap_or("").to_string(),
            })
        }).take(6).collect();
        if !steps.is_empty() {
            return steps;
        }
    }
    Vec::new()
}

/// Split a multi-action sentence into steps WITHOUT the model, resolving
/// "it/them" to what the first step was about:
/// "find my project report, summarise it and email Aman about it" →
/// ["find my project report", "summarise the project report", "email Aman about the project report"].
pub fn decompose(text: &str) -> Vec<String> {
    let parts = crate::reflex::split_compound(text);
    if parts.len() < 2 {
        return parts;
    }
    // the first step's object: its words after the verb, minus "my/the/a"
    let first = simplify(&parts[0]);
    let object: Vec<&str> = first.split_whitespace().skip(1)
        .filter(|w| !["my", "the", "a", "an", "some", "about", "on", "for", "up"].contains(w)).collect();
    let object = if object.is_empty() { String::new() } else { format!("the {}", object.join(" ")) };
    parts.into_iter().enumerate().map(|(i, p)| {
        if i == 0 || object.is_empty() {
            return p;
        }
        let mut out = Vec::new();
        for w in p.split_whitespace() {
            let bare = w.trim_matches(|c: char| !c.is_alphanumeric()).to_lowercase();
            if ["it", "them", "this", "that"].contains(&bare.as_str()) {
                out.push(object.clone());
            } else {
                out.push(w.to_string());
            }
        }
        out.join(" ")
    }).collect()
}

/// "remind me 30 minutes before the first meeting" + an agenda result
/// ("10 AM — Design review; 3 PM — Call with Aman") → (title, "tomorrow at 9:30 AM").
/// The 3B model can't do this arithmetic reliably; code can.
pub fn relative_reminder(goal: &str, earlier: &str, day: &str) -> Option<(String, String)> {
    let g = simplify(goal);
    let words: Vec<&str> = g.split_whitespace().collect();
    let i = words.iter().position(|w| *w == "before")?;
    let n: i64 = words.get(i.checked_sub(2)?)?.parse().ok().or_else(|| match *words.get(i - 2)? { "half" => Some(30), "an" | "one" => Some(60), _ => None })?;
    let unit = *words.get(i - 1)?;
    let mins = if unit.starts_with("hour") { n * 60 } else if unit.starts_with("min") { n } else { return None };
    let pick_last = g.contains("last meeting");
    // agenda lines: "10 AM — Design review", "3:30 PM — Call with Aman"
    let re_items: Vec<(i64, String)> = earlier.split(['\n', ';']).filter_map(|part| {
        let part = part.trim();
        let (time, title) = part.split_once(" — ")?;
        // the LAST time token before the dash: "…2 things: 10 AM" → 10 AM, "3:30 PM" → 3:30 PM
        let toks: Vec<&str> = time.split_whitespace().collect();
        let (num, pm) = match toks.last().map(|t| t.to_uppercase()) {
            Some(ap) if ap == "AM" || ap == "PM" => (*toks.get(toks.len().checked_sub(2)?)?, ap == "PM"),
            Some(t) => (*toks.last()?, t.ends_with("PM")),
            None => return None,
        };
        let num: String = num.chars().filter(|c| c.is_ascii_digit() || *c == ':').collect();
        let (h, m) = match num.split_once(':') {
            Some((h, m)) => (h.parse::<i64>().ok(), m.parse::<i64>().ok()),
            None => (num.parse().ok(), Some(0)),
        };
        let (mut h, m) = (h.filter(|h| *h <= 24)?, m.unwrap_or(0));
        let hh = h;
        if pm && hh < 12 { h += 12 } else if !pm && hh == 12 { h = 0 }
        let title = title.trim().trim_end_matches('.').to_string();
        let title = title.rsplit_once(" (").map(|x| x.0.to_string()).unwrap_or(title);
        Some((h * 60 + m, title))
    }).collect();
    let (start, title) = if pick_last { re_items.iter().max_by_key(|x| x.0)? } else { re_items.iter().min_by_key(|x| x.0)? }.clone();
    let at = (start - mins).max(0);
    let (h, m) = (at / 60, at % 60);
    let (h12, ap) = match h { 0 => (12, "AM"), 1..=11 => (h, "AM"), 12 => (12, "PM"), _ => (h - 12, "PM") };
    Some((title, format!("{day} at {h12}:{m:02} {ap}")))
}

/// Built-in plans for common goals the 3B model plans badly.
/// "prepare for my meeting with Aman tomorrow" → agenda, their mail, their chat, tell me.
pub fn template(text: &str) -> Vec<Step> {
    let s = simplify(text);
    let mk = |g: &str, t: Option<&str>| Step { goal: g.to_string(), tool: t.map(str::to_string), args: serde_json::json!({}), check: String::new() };
    for pre in ["prepare for my meeting with ", "prepare me for my meeting with ", "prepare for the meeting with ", "prepare for my call with "] {
        if let Some(rest) = s.strip_prefix(pre) {
            let day = ["tomorrow", "today", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"].iter().find(|d| rest.contains(*d)).copied().unwrap_or("today");
            let who = rest.split_whitespace().take_while(|w| !["tomorrow", "today", "on", "at", "this", "next"].contains(w)).collect::<Vec<_>>().join(" ");
            if who.is_empty() {
                break;
            }
            return vec![
                mk(&format!("what's on my calendar {day}"), Some("list_agenda")),
                mk(&format!("any mail from {who}"), Some("read_mail")),
                mk(&format!("check what messages {who} has sent me"), Some("read_chat_messages")),
                mk(&format!("tell me what I should know for my meeting with {who}"), None),
            ];
        }
    }
    Vec::new()
}

/// "Three steps: find the report, summarise it, and draft the email."
pub fn spoken(steps: &[Step]) -> String {
    let n = match steps.len() { 2 => "Two", 3 => "Three", 4 => "Four", 5 => "Five", 6 => "Six", _ => "A few" };
    let goals: Vec<String> = steps.iter().map(|s| {
        let g = s.goal.trim_end_matches('.');
        let mut c = g.chars();
        c.next().map(|f| f.to_lowercase().collect::<String>() + c.as_str()).unwrap_or_default()
    }).collect();
    let list = match goals.len() {
        0 => String::new(),
        1 => goals[0].clone(),
        _ => format!("{}, and {}", goals[..goals.len() - 1].join(", "), goals[goals.len() - 1]),
    };
    format!("{n} steps: {list}.")
}

/// A step's args still refer to an earlier result ("the file from step 1")
/// — it can't be run as a single fixed call.
pub fn args_need_context(args: &Value) -> bool {
    let s = args.to_string().to_lowercase();
    s.contains("step ") || s.contains("result") || s.contains("previous") || s.contains("<") || s.contains("{{")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn when_to_plan() {
        assert!(needs_plan("find my project report, summarise it and email it to Aman"));
        assert!(needs_plan("research electric cars and make a presentation about them"));
        assert!(needs_plan("open Discord and go to the friends section and message Aman hi"));
        assert!(!needs_plan("open Discord"));
        assert!(!needs_plan("what's the capital of France"));
        assert!(!needs_plan("send hi to Hatim on Discord"));
    }

    #[test]
    fn relative_reminders_and_templates() {
        let agenda = "Tomorrow you have 2 things: 10 AM — Design review (Zoom); 3 PM — Call with Aman.";
        assert_eq!(relative_reminder("remind me 30 minutes before the first meeting", agenda, "tomorrow"),
                   Some(("Design review".into(), "tomorrow at 9:30 AM".into())));
        assert_eq!(relative_reminder("remind me 1 hour before the last meeting", agenda, "tomorrow"),
                   Some(("Call with Aman".into(), "tomorrow at 2:00 PM".into())));
        assert_eq!(template("prepare for my meeting with Aman tomorrow").len(), 4);
        assert!(template("open discord").is_empty());
    }

    #[test]
    fn decomposes_without_the_model() {
        assert_eq!(decompose("find my project report, summarise it and draft an email to Aman about it"),
            vec!["find my project report", "summarise the project report", "draft an email to Aman about the project report"]);
        assert_eq!(decompose("research electric cars and make a presentation about them"),
            vec!["research electric cars", "make a presentation about the electric cars"]);
        assert_eq!(decompose("open Discord"), vec!["open Discord"]);
    }

    #[test]
    fn parses_line_plans() {
        let raw = "1. find_file | find the project report\n2. - | summarise it\n3. compose_email | draft an email to Aman with the summary";
        let st = parse_plan(raw);
        assert_eq!(st.len(), 3);
        assert_eq!(st[0].tool.as_deref(), Some("find_file"));
        assert_eq!(st[1].tool, None);
        assert_eq!(st[2].goal, "draft an email to Aman with the summary");
        assert_eq!(parse_plan("Sure!\n1) create_presentation | make slides about electric cars").len(), 1);
    }

    #[test]
    fn parses_and_speaks() {
        let raw = r#"Here: {"steps": [{"goal": "Find the project report", "tool": "find_file", "args": {"folder": "~", "partial_name": "project report"}, "check": "a file is found"},
            {"goal": "Summarise the file found in step 1", "tool": "", "args": {}, "check": "a summary"},
            {"goal": "Draft an email to Aman with the summary", "tool": "compose_email", "args": {"recipient": "Aman", "key_points": "the summary from step 2"}, "check": "draft open"}]}"#;
        let steps = parse_plan(raw);
        assert_eq!(steps.len(), 3);
        assert_eq!(steps[0].tool.as_deref(), Some("find_file"));
        assert_eq!(steps[1].tool, None);
        assert!(args_need_context(&steps[2].args));
        assert!(!args_need_context(&steps[0].args));
        assert_eq!(spoken(&steps), "Three steps: find the project report, summarise the file found in step 1, and draft an email to Aman with the summary.");
        assert!(parse_plan("no json here").is_empty());
    }
}
