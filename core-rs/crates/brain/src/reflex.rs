//! Reflexes — requests answered or done with ONE exact action and no model
//! (< 1 ms to decide).  The baseline exam showed the model, given these,
//! made 3 calls (≈8 s) for "open Discord" and added actions nobody asked
//! for (switching tabs, pressing shortcuts).

use serde_json::{json, Value};

use crate::shortcuts;
use crate::utterance::{has_word, simplify};

#[derive(Debug, Clone, PartialEq)]
pub enum Reflex {
    /// Run exactly this tool; `say` is spoken after it succeeds.
    Tool { name: &'static str, args: Value, say: String },
    /// Answer directly (time, date) — no tool, no model.
    Say(String),
    /// A timer / alarm / reminder (kept by DEMASCAS itself).
    Event(events::Request),
    /// A standing permission: "always allow opening apps" (kind: allow | ask | deny).
    Permission { kind: &'static str, subject: String },
    /// "what are your rules?"
    Rules,
    /// "what did you do today?"
    ActionsToday,
    /// "what was the last thing I asked you?"
    LastRequest,
    /// "is Discord open?" — answered from the running apps, not the model
    IsRunning(String),
    /// Several exact steps in order (e.g. search YouTube, then play the first result).
    Steps { steps: Vec<(&'static str, Value)>, say: String },
    /// Find a named file, then open or delete it (delete asks first), or
    /// just say where it is.
    File { action: FileAction, name: String, folder: Option<String> },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FileAction {
    Open,
    Delete,
    Find,
    /// find it, read it, summarise it ("summarise my CV")
    Read,
    /// "how many PNG files are on my desktop" / "list the PDFs in downloads":
    /// `name` is the extension ("" = every file), `folder` is required.
    List,
    /// the same, answered with just the number ("how many…")
    Count,
    /// open a random file of a kind: `name` = extensions ("png,jpg"), folder optional
    Random,
}

impl Reflex {
    /// Reaches other people or deletes something — needs clearly heard speech.
    pub fn is_outward(&self) -> bool {
        match self {
            Reflex::Tool { name, .. } => ["message_contact", "compose_email", "quit_application", "call_contact"].contains(name),
            Reflex::File { action, .. } => *action == FileAction::Delete,
            _ => false,
        }
    }

    fn tool(name: &'static str, args: Value, say: impl Into<String>) -> Self {
        Reflex::Tool { name, args, say: say.into() }
    }
}

/// What the conversation already established ("close it" → the app just opened).
#[derive(Debug, Clone, Default)]
pub struct Recent {
    pub last_app: Option<String>,
    /// The last reflex tool ("and in Mumbai?" repeats a weather lookup).
    pub last_tool: Option<String>,
    /// Installed + running app names (from the tool server).  Empty = unknown:
    /// then any plausible name is accepted.
    pub apps: std::sync::Arc<Vec<String>>,
    pub clock: events::Clock,
    /// The file last found / opened ("show it to me").
    pub last_file: Option<String>,
}

fn app_key(s: &str) -> String {
    s.chars().filter(|c| c.is_alphanumeric()).flat_map(char::to_lowercase).collect()
}

/// A spoken name → a real app ("powerpoint" → "Microsoft PowerPoint").
/// Exact name first, then a whole-word suffix/prefix covering ≥ half the name.
pub fn resolve_app(spoken: &str, apps: &[String]) -> Option<String> {
    let k = app_key(spoken);
    if k.len() < 2 {
        return None;
    }
    if let Some(a) = apps.iter().find(|a| app_key(a) == k) {
        return Some(a.clone());
    }
    let mut best: Option<(&String, usize)> = None;
    for a in apps {
        let ak = app_key(a);
        let words: Vec<String> = a.split_whitespace().map(app_key).collect();
        let whole = words.iter().any(|w| *w == k) || words.join("") == k;
        if k.len() >= 4 && (ak.ends_with(&k) || ak.starts_with(&k) || whole) && k.len() * 2 >= ak.len() {
            if best.is_none_or(|(_, l)| ak.len() < l) {
                best = Some((a, ak.len()));
            }
        }
    }
    if let Some((a, _)) = best {
        return Some(a.clone());
    }
    // how it SOUNDS: "this card" / "this cord" → Discord
    let sk = crate::normalize::skeleton(spoken);
    if sk.len() >= 3 {
        if let Some(a) = apps.iter().find(|a| crate::normalize::skeleton(a) == sk) {
            return Some(a.clone());
        }
    }
    None
}

const BROWSERS: &[(&str, &str)] = &[("brave", "Brave Browser"), ("safari", "Safari"), ("chrome", "Google Chrome"), ("firefox", "Firefox"), ("arc", "Arc"), ("edge", "Microsoft Edge")];
const SITES: &[(&str, &str)] = &[
    ("youtube", "https://www.youtube.com"), ("google", "https://www.google.com"), ("github", "https://github.com"),
    ("wikipedia", "https://en.wikipedia.org"), ("gmail", "https://mail.google.com"), ("twitter", "https://x.com"),
    ("reddit", "https://www.reddit.com"), ("netflix", "https://www.netflix.com"), ("amazon", "https://www.amazon.in"),
    ("chatgpt", "https://chatgpt.com"), ("linkedin", "https://www.linkedin.com"), ("instagram", "https://www.instagram.com"),
];

fn url_encode(s: &str) -> String {
    s.bytes().map(|b| match b {
        b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => (b as char).to_string(),
        b' ' => "+".into(),
        _ => format!("%{b:02X}"),
    }).collect()
}

/// Websites and YouTube: "open youtube.com", "open google.com in Safari",
/// "open Wikipedia on Brave", "search YouTube for lofi jazz", "play lofi on YouTube".
fn web_reflex(raw: &str, s: &str) -> Option<Reflex> {
    let browser = BROWSERS.iter().find(|(k, _)| s.split_whitespace().any(|w| w == *k)).map(|(_, b)| b.to_string()).unwrap_or_default();
    let strip_browser = |t: &str| {
        let mut t = t.to_string();
        for (k, _) in BROWSERS {
            for pre in [" in ", " on ", " using ", " with "] {
                for suffix in [format!("{pre}{k} browser"), format!("{pre}{k}")] {
                    t = t.replace(&suffix, "");
                }
            }
        }
        t.trim().to_string()
    };
    // search / play on YouTube
    if let Some(q) = ["search youtube for ", "search on youtube for ", "youtube search for ", "look up on youtube "].iter().find_map(|p| s.strip_prefix(p)) {
        let q = q.trim();
        if !q.is_empty() {
            return Some(Reflex::tool("open_url", json!({"url": format!("https://www.youtube.com/results?search_query={}", url_encode(q)), "browser": browser}), format!("Here are YouTube results for {q}.")));
        }
    }
    let play = s.strip_prefix("play ").map(|r| r.trim_end_matches(" on youtube").trim_end_matches(" from youtube"));
    if let Some(q) = play.filter(|_| s.contains("youtube") || s.contains("music") || s.contains("song")) {
        let q = q.trim_start_matches("some ").trim();
        if !q.is_empty() && q.split_whitespace().count() <= 8 {
            return Some(Reflex::Steps { steps: vec![("web_youtube_search", json!({"query": q})), ("web_youtube_play_first", json!({}))], say: format!("Playing {q}.") });
        }
    }
    // open a site
    let rest = ["open ", "go to ", "visit ", "launch ", "browse to ", "take me to "].iter().find_map(|p| s.strip_prefix(p))?;
    let rest = strip_browser(rest);
    let addr = raw.split_whitespace().map(|w| w.trim_matches(|c: char| ",!?\"'".contains(c)).trim_end_matches('.')).find(|w| {
        w.rsplit_once('.').is_some_and(|(a, b)| !a.is_empty() && (2..=6).contains(&b.len()) && b.chars().all(|c| c.is_ascii_alphabetic()))
            && !w.contains('/') || w.starts_with("http")
    });
    let url = match addr {
        Some(a) if a.starts_with("http") => a.to_string(),
        Some(a) => format!("https://{}", a.to_lowercase()),
        None => SITES.iter().find(|(k, _)| rest == *k || rest == format!("{k} website") || rest == format!("{k} site") || rest == format!("{k} com") || rest == format!("{k} dot com")).map(|(_, u)| u.to_string())?,
    };
    let name = url.trim_start_matches("https://").trim_start_matches("http://").trim_start_matches("www.").to_string();
    Some(Reflex::tool("open_url", json!({"url": url, "browser": browser}), format!("Opening {name}.")))
}

/// Messages, email drafts, clipboard.
fn message_reflex(raw: &str, s: &str) -> Option<Reflex> {
    let first = s.split(' ').next().unwrap_or("");
    // "copy hello world to my clipboard"
    if first == "copy" {
        for tail in [" to my clipboard", " to the clipboard", " to clipboard"] {
            if let Some(p) = raw.to_lowercase().find(tail) {
                let text = raw[4..p].trim().trim_matches(|c: char| c == '"' || c == '\'');
                if !text.is_empty() {
                    return Some(Reflex::tool("set_clipboard", json!({"text": text}), "Copied."));
                }
            }
        }
    }
    // "write an email to john@x.com about the meeting tomorrow" — a DRAFT, never sent
    for pre in ["write an email to ", "write a mail to ", "compose an email to ", "draft an email to ", "email ", "write email to ", "send an email to "] {
        if let Some(rest) = raw.to_lowercase().strip_prefix(pre).map(|_| raw[pre.len()..].trim()) {
            let lower = rest.to_lowercase();
            let (to, about) = ["about", "regarding", "saying", "that", "to say", "telling him", "telling her", "telling them",
                "to tell him", "to tell her", "letting him know", "letting her know", "and tell him", "and tell her"].iter()
                .find_map(|k| lower.find(&format!(" {k} ")).map(|i| (rest[..i].trim(), rest[i + k.len() + 2..].trim())))
                .unwrap_or((rest, ""));
            // "email Aman a summary (of it)" → to Aman
            let to = ["a summary of it", "a summary", "the summary", "a quick summary"].iter()
                .find_map(|t| to.to_lowercase().find(t).map(|i| to[..i].trim())).unwrap_or(to);
            if !to.is_empty() && to.split_whitespace().count() <= 4 {
                return Some(Reflex::tool("compose_email", json!({"recipient": to, "subject": about, "key_points": about}), ""));
            }
        }
    }
    // "type hello everyone and press enter" — typing then sending = asks first
    if first == "type" {
        for tail in [" and press enter", " and hit enter", " and press return", " and send it", " and send"] {
            if let Some(p) = raw.to_lowercase().find(tail) {
                let text = raw[4..p].trim().trim_matches(|c: char| c == '"' || c == '\'');
                if !text.is_empty() {
                    return Some(Reflex::tool("type_and_submit", json!({"text": text}), "Sent."));
                }
            }
        }
    }
    // "type buy milk" — type exactly that, nothing else
    // …but "type X in the name field" names a field on screen — that's screen work (ui_type)
    let into_field = [" field", " box", " textbox", " text box", " input"].iter().any(|f| s.contains(f))
        && [" in the ", " into the ", " in my ", " in ", " into "].iter().any(|p| s.contains(p));
    if first == "type" && !into_field && !s.contains(" and press ") && !s.contains(" and send") && !s.contains(" and hit ") {
        let text = raw.trim()[4..].trim().trim_matches(|c: char| c == '"' || c == '\'');
        if !text.is_empty() && text.split_whitespace().count() <= 30 {
            return Some(Reflex::tool("type_text", json!({"text": text}), "Typed."));
        }
    }
    // chat messages: always the read-back-then-yes flow
    const VERBS: &[&str] = &["send", "dm", "message", "text", "reply", "ping", "tell", "say"];
    if !VERBS.contains(&first) || s.starts_with("tell me") || s.starts_with("say that again") || s.contains("email") {
        return None;
    }
    let to_someone = s.contains(" to ") || s.contains(" saying ") || s.contains(" that ") || first == "dm" || first == "message" || first == "text" || first == "ping" || first == "reply";
    let names_chat = CHAT_APPS.iter().any(|a| s.split_whitespace().any(|w| w == *a));
    if to_someone && (names_chat || first != "say") && s.split_whitespace().count() >= 3 {
        return Some(Reflex::tool("message_contact", json!({"request": raw}), ""));
    }
    None
}

fn governance_reflex(s: &str) -> Option<Reflex> {
    const TODAY: &[&str] = &["what did you do today", "what have you done today", "what did you do", "what have you done", "what actions did you take", "show me what you did today"];
    if TODAY.contains(&s) {
        return Some(Reflex::ActionsToday);
    }
    // "is Discord open", "is Safari running", "is spotify still open"
    if let Some(rest) = s.strip_prefix("is ") {
        for tail in [" still running", " still open", " running", " open right now", " open", " on"] {
            if let Some(app) = rest.strip_suffix(tail) {
                let app = app.trim();
                let not_app = ["it", "that", "this", "there", "anything", "something", "everything", "the file", "the door", "the shop", "the store"].contains(&app)
                    || ["internet", "wifi", "wi fi", "network", "bluetooth", "light", "lights", "tv", "window", "tab", "file", "document", "store", "shop", "bank", "office", "school", "restaurant", "today", "tomorrow"]
                        .iter().any(|w| app.split_whitespace().any(|x| x == *w) || app.contains("wi fi"));
                if !app.is_empty() && app.split_whitespace().count() <= 3 && !not_app {
                    return Some(Reflex::IsRunning(app.to_string()));
                }
            }
        }
    }
    const LAST: &[&str] = &["what was the last thing i asked", "what did i just ask", "what did i ask you last", "what was my last request", "what did i ask you to do"];
    if LAST.iter().any(|p| s.starts_with(p)) {
        return Some(Reflex::LastRequest);
    }
    const RULES: &[&str] = &["what are your permissions", "what are your rules", "what can you do without asking", "what do you ask before doing", "what are you allowed to do"];
    if RULES.contains(&s) {
        return Some(Reflex::Rules);
    }
    let forms: &[(&str, &'static str)] = &[
        ("always allow ", "allow"), ("you can always ", "allow"), ("dont ask before ", "allow"), ("dont ask me before ", "allow"),
        ("no need to ask before ", "allow"), ("stop asking before ", "allow"),
        ("always ask before ", "ask"), ("always ask me before ", "ask"), ("ask me before ", "ask"), ("check with me before ", "ask"),
        ("never do ", "deny"), ("dont ever ", "deny"), ("you are not allowed to ", "deny"), ("never ", "deny"),
    ];
    for (pre, kind) in forms {
        if let Some(subject) = s.strip_prefix(pre) {
            if *pre == "never " && (subject.starts_with("mind") || subject.split_whitespace().count() < 1) {
                return None;
            }
            let (kind, subject) = match subject.strip_suffix(" without asking").or_else(|| subject.strip_suffix(" without asking me")) {
                Some(sub) if *kind == "deny" => ("ask", sub),
                _ => (*kind, subject),
            };
            if crate::governance::tools_for(subject).is_some() {
                return Some(Reflex::Permission { kind, subject: subject.to_string() });
            }
        }
    }
    None
}

fn day_in(s: &str) -> String {
    for d in ["day after tomorrow", "tomorrow", "today", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"] {
        if s.contains(d) {
            return if s.contains(&format!("next {d}")) { format!("next {d}") } else { d.to_string() };
        }
    }
    if s.contains("this week") { "this week".into() } else { "today".into() }
}

/// Calendar / Reminders / Contacts / Mail / Notes requests → one tool.
fn apple_reflex(raw: &str, s: &str) -> Option<Reflex> {
    // agenda
    const AGENDA: &[&str] = &["whats on my calendar", "what is on my calendar", "what do i have", "whats my schedule", "what is my schedule",
        "my schedule", "any meetings", "do i have any meetings", "what meetings do i have", "my agenda", "whats on my agenda", "show my calendar", "check my calendar"];
    if AGENDA.iter().any(|p| s.starts_with(p)) && !s.contains(" add ") {
        let day = day_in(s);
        let (day, days) = if day == "this week" { ("today".to_string(), 7) } else { (day, 1) };
        return Some(Reflex::tool("list_agenda", json!({"day": day, "days": days}), ""));
    }
    // reminders list
    const REMS: &[&str] = &["what are my reminders", "show my reminders", "my reminders", "what reminders do i have", "whats on my to do list", "what do i need to do", "read my reminders", "my to do list"];
    if REMS.iter().any(|p| s.starts_with(p)) {
        return Some(Reflex::tool("list_reminders", json!({}), ""));
    }
    // add to calendar: "add a meeting with Aman tomorrow at 3 pm to my calendar", "schedule a call with mom on friday at 6"
    let to_cal = s.ends_with(" to my calendar") || s.ends_with(" on my calendar") || s.ends_with(" in my calendar");
    if (s.starts_with("schedule ") || s.starts_with("book ") || ((s.starts_with("add ") || s.starts_with("put ")) && to_cal)) && !s.contains("reminder") {
        let body = s.trim_end_matches(" to my calendar").trim_end_matches(" on my calendar").trim_end_matches(" in my calendar");
        let body = body.split_once(' ').map(|x| x.1).unwrap_or("");
        let cut = [" tomorrow", " today", " at ", " on ", " next ", " this ", " in "].iter().filter_map(|m| body.find(m)).min();
        if let Some(c) = cut {
            let title = body[..c].trim_start_matches("a ").trim_start_matches("an ").trim();
            let when = body[c..].trim();
            if !title.is_empty() && !when.is_empty() {
                // keep the user's capitals ("Aman", not "aman")
                let title = raw.to_lowercase().find(title).map(|i| raw[i..i + title.len()].to_string()).unwrap_or_else(|| title.to_string());
                return Some(Reflex::tool("add_event", json!({"title": title, "when": when}), ""));
            }
        }
    }
    // "what can't you access?" / "check your permissions"
    if ["what cant you access", "what can you not access", "check your permissions", "check permissions", "what permissions do you have",
        "do you have all the permissions", "what permissions are missing", "which permissions are missing"].iter().any(|p| s.starts_with(p)) {
        return Some(Reflex::tool("check_permissions", json!({}), ""));
    }
    // calls: "who called me", "missed calls", "list the first five contacts I have called"
    if let Some(r) = calls_reflex(&s) {
        return Some(r);
    }
    // "call Vijval", "FaceTime mom", "call the person whose name is spelled Vijval"
    if let Some(r) = call_reflex(&s, raw) {
        return Some(r);
    }
    // contacts: "what's Aman's number", "what is mom's email"
    for kind in ["s phone number", "s number", "s email address", "s email", "s phone", "s mobile number"] {
        if let Some(p) = s.find(kind).filter(|_| s.starts_with("what") || s.starts_with("give me") || s.starts_with("tell me") || s.starts_with("find")) {
            let before = &s[..p];
            let who = before.rsplit(|c: char| !c.is_alphanumeric() && c != ' ').next().unwrap_or("");
            let who = who.trim_start_matches("whats ").trim_start_matches("what is ").trim_start_matches("give me ").trim_start_matches("tell me ").trim_start_matches("find ").trim();
            if !who.is_empty() && who.split_whitespace().count() <= 3 {
                return Some(Reflex::tool("find_contact", json!({"name": who}), ""));
            }
        }
    }
    // mail
    const MAIL: &[&str] = &["read my email", "read my emails", "read my mail", "read my latest email", "read my latest emails", "read my new email",
        "read my new emails", "check my email", "check my emails", "check my mail", "check my inbox", "any new email", "any new emails", "any new mail",
        "do i have any new email", "do i have new mail", "whats in my inbox", "what is in my inbox", "read my unread emails"];
    if MAIL.iter().any(|p| s.starts_with(p)) {
        return Some(Reflex::tool("read_mail", json!({"count": 5, "unread_only": true}), ""));
    }
    for pre in ["any mail from ", "any email from ", "any emails from ", "did i get an email from ", "did i get mail from ", "read emails from ", "read the email from ", "read mail from "] {
        if let Some(who) = s.strip_prefix(pre) {
            let who = who.trim();
            if !who.is_empty() && who.split_whitespace().count() <= 3 {
                return Some(Reflex::tool("read_mail", json!({"count": 5, "unread_only": false, "sender": who}), ""));
            }
        }
    }
    if let Some(who) = s.strip_prefix("did ").and_then(|r| r.strip_suffix(" email me").or_else(|| r.strip_suffix(" mail me"))) {
        return Some(Reflex::tool("read_mail", json!({"count": 5, "unread_only": false, "sender": who.trim()}), ""));
    }
    // "what are the latest emails I received", "can you read me the latest mail
    // I got on Gmail" — reading mail, said any way (never writing or sending it)
    {
        let w: Vec<&str> = s.split_whitespace().collect();
        let about_mail = w.iter().any(|x| ["mail", "mails", "email", "emails", "inbox", "gmail"].contains(x));
        let reading = w.iter().any(|x| ["read", "latest", "recent", "new", "unread", "received", "got", "check", "show", "last"].contains(x));
        let writing = w.iter().any(|x| ["write", "send", "draft", "compose", "reply", "forward", "delete", "to", "about", "saying"].contains(x))
            && !s.contains("sent to me") && !s.contains("received");
        if about_mail && reading && !writing && w.len() <= 16 {
            let unread = w.iter().any(|x| ["new", "unread"].contains(x));
            let mut args = json!({"count": 5, "unread_only": unread});
            if w.iter().any(|x| ["gmail", "brave", "browser", "chrome"].contains(x)) {
                args["source"] = json!("gmail");
            }
            return Some(Reflex::tool("read_mail", args, ""));
        }
    }
    // notes: "add milk to my shopping note/list", "take a note: …", "note that …", "read my notes about …"
    if let Some(rest) = s.strip_prefix("add ") {
        for tail in [" note", " list", " notes"] {
            if let Some(p) = rest.rfind(" to my ") {
                let (item, target) = (&rest[..p], &rest[p + 7..]);
                if let Some(title) = target.strip_suffix(tail) {
                    if !item.is_empty() && !title.is_empty() {
                        let raw_item = raw.split_whitespace().skip(1).take(item.split_whitespace().count()).collect::<Vec<_>>().join(" ");
                        let mut t = title.chars();
                        let title = t.next().map(|f| f.to_uppercase().collect::<String>() + t.as_str()).unwrap_or_default();
                        return Some(Reflex::tool("add_note", json!({"text": raw_item, "title": title}), ""));
                    }
                }
            }
        }
    }
    for pre in ["take a note that ", "take a note ", "make a note that ", "make a note ", "note that ", "write down that ", "write down "] {
        if s.starts_with(pre) {
            let text = raw.trim()[pre.len().min(raw.trim().len())..].trim().trim_start_matches([':', ',']).trim();
            if !text.is_empty() {
                return Some(Reflex::tool("add_note", json!({"text": text}), ""));
            }
        }
    }
    for pre in ["read my notes about ", "what are my notes about ", "find my notes about ", "whats in my ", "read my "] {
        if let Some(q) = s.strip_prefix(pre) {
            let q = q.trim_end_matches(" note").trim_end_matches(" notes").trim_end_matches(" list").trim();
            if !q.is_empty() && (pre.contains("notes") || s.ends_with(" note") || s.ends_with(" list")) && q.split_whitespace().count() <= 4 {
                return Some(Reflex::tool("read_notes", json!({"query": q}), ""));
            }
        }
    }
    None
}

/// The chat apps whose messages DEMASCAS can read.
const CHAT_APPS: &[&str] = &["discord", "slack", "whatsapp", "messages", "telegram", "signal", "teams", "messenger"];

/// "check what messages Krishmithal has sent me (on Discord)", "read my
/// messages from Aman", "what did Aman say on Slack" → (app, contact).
pub fn read_messages_request(text: &str) -> Option<(String, String)> {
    let s = simplify(text);
    let mut app = String::new();
    let mut rest = s.clone();
    for a in CHAT_APPS {
        for prep in ["on", "in", "from"] {
            let tail = format!(" {prep} {a}");
            if let Some(p) = rest.find(&tail) {
                app = a.to_string();
                rest = format!("{}{}", &rest[..p], &rest[p + tail.len()..]);
            }
        }
    }
    let rest = rest.trim();
    let patterns: &[(&str, &str)] = &[
        ("check what messages ", " has sent me"), ("check what messages ", " sent me"), ("check what ", " has sent me"),
        ("check what ", " sent me"), ("read what ", " sent me"), ("read what ", " has sent me"), ("what messages has ", " sent me"),
        ("what has ", " sent me"), ("what did ", " send me"), ("what did ", " say"), ("check messages from ", ""),
        ("check my messages from ", ""), ("read messages from ", ""), ("read my messages from ", ""), ("read the messages from ", ""),
        ("show me messages from ", ""), ("any messages from ", ""), ("did i get any messages from ", ""), ("check the messages from ", ""),
    ];
    for (pre, post) in patterns {
        if let Some(after) = rest.strip_prefix(pre) {
            let who = if post.is_empty() { after } else { after.strip_suffix(post).unwrap_or("") };
            let who = who.trim().trim_start_matches("the ").trim();
            if !who.is_empty() && who.split_whitespace().count() <= 3 && !["me", "anyone", "everyone"].contains(&who) {
                let title = who.split_whitespace().map(|w| { let mut c = w.chars(); c.next().map(|f| f.to_uppercase().collect::<String>() + c.as_str()).unwrap_or_default() }).collect::<Vec<_>>().join(" ");
                let app = if app.is_empty() { String::new() } else { let mut c = app.chars(); c.next().map(|f| f.to_uppercase().collect::<String>() + c.as_str()).unwrap_or_default() };
                let app = if app == "Whatsapp" { "WhatsApp".to_string() } else { app };
                return Some((app, title));
            }
        }
    }
    None
}

/// "X, and Y" / "X and then Y" → [X, Y] when Y is a request of its own
/// (starts with a verb or a question word) — not "salt and pepper".
/// "open the presentation and show it to me" stays one request: showing is
/// what opening does.
pub fn split_compound(text: &str) -> Vec<String> {
    const SHOW: &[&str] = &["show it to me", "show it", "show me", "show me it", "let me see it", "show it on screen", "display it"];
    // "if I have 3 boxes…, how many are left" / "when it rains, remind me…" —
    // a clause joined to its main sentence is ONE request
    const CLAUSE: &[&str] = &["if", "when", "whenever", "suppose", "supposing", "assuming", "since", "because", "although", "though", "once", "unless", "given"];
    if CLAUSE.contains(&simplify(text).split(' ').next().unwrap_or("")) {
        return vec![text.to_string()];
    }
    let mut parts = split_parts(text);
    let mut i = 1;
    while i < parts.len() {
        let opens = simplify(&parts[i - 1]).starts_with("open ");
        if opens && SHOW.contains(&simplify(&parts[i]).as_str()) {
            parts.remove(i);
        } else {
            i += 1;
        }
    }
    parts
}

fn split_parts(text: &str) -> Vec<String> {
    const STARTS: &[&str] = &[
        "what", "whats", "who", "when", "where", "how", "is", "are", "tell", "open", "close", "quit", "check",
        "read", "send", "play", "set", "remind", "find", "show", "search", "go", "switch", "type", "take",
        "turn", "start", "stop", "call", "message", "text", "remember", "delete", "create", "make", "list",
        "summarise", "summarize", "draft", "email", "write", "compose", "research", "save", "move", "copy",
        "add", "translate", "attach", "forward", "reply", "share", "upload", "download", "prepare", "build",
    ];
    let lower = text.to_lowercase();
    // the EARLIEST place where a new request begins
    let mut best: Option<(usize, usize)> = None;
    for sep in [", and then ", " and then ", ", and ", ", then ", " and ", ", "] {
        let mut from = 0;
        while let Some(off) = lower[from..].find(sep) {
            let p = from + off;
            let (a, b) = (text[..p].trim(), text[p + sep.len()..].trim());
            let bw = simplify(b);
            let first = bw.split(' ').next().unwrap_or("");
            if a.split_whitespace().count() >= 2 && bw.split_whitespace().count() >= 2 && STARTS.contains(&first) {
                if best.is_none_or(|(bp, _)| p < bp) {
                    best = Some((p, sep.len()));
                }
                break;
            }
            from = p + sep.len();
        }
    }
    match best {
        Some((p, n)) => {
            let mut out = vec![text[..p].trim().trim_end_matches(',').to_string()];
            out.extend(split_parts(text[p + n..].trim()));
            out
        }
        None => vec![text.to_string()],
    }
}

/// Words that mean "a file", not an app.
const FILE_WORDS: &[&str] = &[
    "file", "document", "doc", "pdf", "resume", "cv", "report", "presentation", "slides", "deck", "spreadsheet",
    "sheet", "photo", "picture", "image", "screenshot", "notes", "essay", "assignment", "invoice", "receipt",
    "letter", "contract", "video", "recording",
];

fn folder_of(s: &str) -> Option<String> {
    for (w, f) in [("desktop", "~/Desktop"), ("downloads", "~/Downloads"), ("download", "~/Downloads"), ("documents", "~/Documents"),
                   ("document folder", "~/Documents"), ("pictures", "~/Pictures"), ("picture", "~/Pictures"), ("photos folder", "~/Pictures"),
                   ("music", "~/Music"), ("movies", "~/Movies"), ("videos folder", "~/Movies")] {
        if w.contains(' ') {
            if s.contains(w) {
                return Some(f.to_string());
            }
            continue;
        }
        if s.split_whitespace().any(|x| x == w) {
            return Some(f.to_string());
        }
    }
    None
}

/// "open any random PNG file from the pictures folder" / "take any random
/// presentation on the laptop and open it" → a random file of that kind.
fn random_file(s: &str) -> Option<Reflex> {
    if !(has_word(s, "random") || has_word(s, "any") && (has_word(s, "one") || has_word(s, "file"))) {
        return None;
    }
    if !["open", "show", "play", "take", "pick", "choose", "launch"].iter().any(|v| has_word(s, v)) {
        return None;
    }
    const KINDS: &[(&[&str], &str)] = &[
        (&["png", "pngs"], "png"), (&["jpg", "jpeg", "jpgs"], "jpg,jpeg"), (&["pdf", "pdfs"], "pdf"), (&["docx", "word"], "docx,doc"),
        (&["presentation", "presentations", "slides", "powerpoint", "ppt", "pptx", "deck", "keynote"], "pptx,key,ppt"),
        (&["image", "images", "picture", "pictures", "photo", "photos", "pic", "pics", "screenshot", "screenshots", "wallpaper"], "png,jpg,jpeg,heic"),
        (&["document", "documents", "doc"], "pdf,docx,doc,pages"), (&["video", "videos", "movie", "movies", "clip"], "mp4,mov,m4v"),
        (&["song", "songs", "music", "audio", "mp3"], "mp3,m4a,wav"), (&["spreadsheet", "sheet", "excel", "xlsx"], "xlsx,csv,numbers"),
    ];
    // the folder named in the request decides "pictures" = folder vs kind
    let folder = folder_of(s);
    let mut kind = None;
    for w in s.split_whitespace() {
        if let Some((_, e)) = KINDS.iter().find(|(ws, _)| ws.contains(&w)) {
            // "from the pictures folder" names a folder, not a kind
            if folder.as_deref() == Some("~/Pictures") && ["picture", "pictures"].contains(&w) && kind.is_some() {
                continue;
            }
            kind = Some(*e);
            if !["picture", "pictures", "document", "documents", "music", "movies"].contains(&w) {
                break;
            }
        }
    }
    let kind = kind.or_else(|| has_word(s, "file").then_some(""))?;
    Some(Reflex::File { action: FileAction::Random, name: kind.to_string(), folder })
}

/// "what is two plus two times three" / "calculate 36 minus 7" / "how much is
/// 12 divided by 4" → the exact value (×÷ before +−).  None unless the whole
/// rest is numbers and operators.
fn spoken_math(s: &str) -> Option<String> {
    let rest = ["what is ", "whats ", "calculate ", "compute ", "how much is ", "work out ", "solve "]
        .iter().find_map(|p| s.strip_prefix(p))?;
    const UNITS: &[(&str, f64)] = &[
        ("zero", 0.), ("one", 1.), ("two", 2.), ("three", 3.), ("four", 4.), ("five", 5.), ("six", 6.), ("seven", 7.), ("eight", 8.),
        ("nine", 9.), ("ten", 10.), ("eleven", 11.), ("twelve", 12.), ("thirteen", 13.), ("fourteen", 14.), ("fifteen", 15.),
        ("sixteen", 16.), ("seventeen", 17.), ("eighteen", 18.), ("nineteen", 19.), ("twenty", 20.), ("thirty", 30.), ("forty", 40.),
        ("fifty", 50.), ("sixty", 60.), ("seventy", 70.), ("eighty", 80.), ("ninety", 90.),
    ];
    let words: Vec<&str> = rest.split_whitespace().filter(|w| !["and", "the", "of"].contains(w)).collect();
    // tokens: numbers and operators
    let (mut nums, mut ops): (Vec<f64>, Vec<char>) = (Vec::new(), Vec::new());
    let mut cur: Option<f64> = None;
    let mut i = 0;
    let push_num = |cur: &mut Option<f64>, nums: &mut Vec<f64>| -> bool {
        match cur.take() { Some(v) => { nums.push(v); true } None => false }
    };
    while i < words.len() {
        let w = words[i];
        let two = words.get(i + 1).map(|n| format!("{w} {n}"));
        let op = match (w, two.as_deref()) {
            (_, Some("multiplied by")) | (_, Some("divided by")) => {
                i += 1;
                Some(if w == "multiplied" { '*' } else { '/' })
            }
            ("plus" | "+", _) => Some('+'),
            ("minus" | "-" | "less", _) => Some('-'),
            ("times" | "x" | "*" | "into", _) => Some('*'),
            ("over" | "/" | "by", _) => Some('/'),
            _ => None,
        };
        if let Some(o) = op {
            if !push_num(&mut cur, &mut nums) || nums.len() != ops.len() + 1 {
                return None;
            }
            ops.push(o);
        } else if let Ok(v) = w.parse::<f64>() {
            if cur.is_some() {
                return None;
            }
            cur = Some(v);
        } else if let Some((_, v)) = UNITS.iter().find(|(k, _)| *k == w) {
            cur = Some(cur.unwrap_or(0.) + v);
        } else if w == "hundred" {
            cur = Some(cur? * 100.);
        } else if w == "thousand" {
            cur = Some(cur? * 1000.);
        } else {
            return None;
        }
        i += 1;
    }
    if !push_num(&mut cur, &mut nums) || ops.is_empty() || nums.len() != ops.len() + 1 {
        return None;
    }
    // × ÷ first, then + −
    let (mut n2, mut o2) = (vec![nums[0]], Vec::new());
    for (k, &o) in ops.iter().enumerate() {
        let b = nums[k + 1];
        match o {
            '*' => *n2.last_mut()? *= b,
            '/' => { if b == 0. { return Some("undefined — you can't divide by zero".into()); } *n2.last_mut()? /= b }
            _ => { o2.push(o); n2.push(b); }
        }
    }
    let mut v = n2[0];
    for (k, &o) in o2.iter().enumerate() {
        if o == '+' { v += n2[k + 1] } else { v -= n2[k + 1] }
    }
    Some(if (v - v.round()).abs() < 1e-9 { format!("{}", v.round() as i64) } else { format!("{:.4}", v).trim_end_matches('0').to_string() })
}

/// "open an image which looks like a QR code from the pictures folder" →
/// find_image_by_content (Apple Vision looks inside each picture).
fn image_by_content(s: &str) -> Option<Reflex> {
    let verb = ["open", "find", "show", "search for", "look for", "which"].iter().find(|v| s.starts_with(*v))?;
    if !["image", "images", "picture", "pictures", "photo", "photos", "screenshot", "screenshots", "pic", "png", "jpg"].iter().any(|w| has_word(s, w)) {
        return None;
    }
    let marker = ["looks like", "look like", "looking like", "that has", "which has", "with a", "with an", "showing", "containing", "of a", "of an"]
        .iter().find_map(|m| s.find(&format!(" {m} ")).map(|i| i + m.len() + 2))?;
    let mut what = s[marker..].to_string();
    for cut in [" from ", " in my ", " in the ", " in ", " on my ", " on the "] {
        if let Some(i) = what.find(cut) {
            what.truncate(i);
        }
    }
    let what = what.trim();
    let what = ["a ", "an ", "the ", "some "].iter().find_map(|a| what.strip_prefix(a)).unwrap_or(what).to_string();
    if what.is_empty() || what.split_whitespace().count() > 5 {
        return None;
    }
    let folder = folder_of(s).unwrap_or_else(|| "~/Pictures".into());
    Some(Reflex::tool("find_image_by_content", json!({"folder": folder, "what": what, "open": *verb == "open"}), ""))
}

/// "who called me" / "my missed calls" / "list the last five contacts I called".
fn calls_reflex(s: &str) -> Option<Reflex> {
    let missed = has_word(s, "missed call") || has_word(s, "missed calls") || has_word(s, "did i miss");
    let history = missed
        || ["who called me", "who has called me", "who just called", "who was calling", "recent calls", "call history", "call log",
            "last calls", "latest calls", "who did i call", "who have i called", "contacts i have called", "contacts i called",
            "people i called", "people i have called", "my calls", "last few calls"].iter().any(|p| has_word(s, p));
    if !history {
        return None;
    }
    const N: &[(&str, u32)] = &[("one", 1), ("two", 2), ("three", 3), ("four", 4), ("five", 5), ("six", 6), ("seven", 7), ("eight", 8), ("nine", 9), ("ten", 10)];
    let count = s.split_whitespace().find_map(|w| w.parse::<u32>().ok().or_else(|| N.iter().find(|(k, _)| *k == w).map(|(_, n)| *n))).unwrap_or(5).clamp(1, 20);
    Some(Reflex::tool("recent_calls", json!({"count": count, "missed_only": missed}), ""))
}

/// "call Vijval" / "FaceTime mom" / "call the person whose name is spelled
/// Vijval" → call_contact.  The tool server finds the person and asks
/// "Call X on Y?" before anything rings (or asks who, if it can't tell).
fn call_reflex(s: &str, raw: &str) -> Option<Reflex> {
    let (verb, rest) = ["call ", "ring ", "dial ", "phone ", "facetime ", "make a call to ", "place a call to ", "video call ", "audio call "]
        .iter().find_map(|v| s.strip_prefix(v).map(|r| (v.trim(), r.trim())))?;
    // "call me Daksh", "call it a day", "call this function" aren't calls
    let first = rest.split_whitespace().next().unwrap_or("");
    if ["me", "it", "this", "that", "the function", "a", "an", "back"].contains(&first) && !rest.starts_with("a call") {
        return None;
    }
    let video = verb == "video call" || has_word(s, "video");
    let facetime = verb == "facetime" || has_word(s, "facetime") || has_word(s, "face time") || video;
    let via = if video { "facetime" } else if facetime { "facetime_audio" } else { "phone" };
    let mut name = rest.to_string();
    for p in ["the person whose name is spelled ", "the person whose name is ", "the person named ", "the person called ",
              "someone named ", "someone called ", "my friend ", "the contact ", "contact "] {
        if let Some(i) = name.find(p) {
            name = name[i + p.len()..].to_string();
        }
    }
    for t in [" on facetime video", " on facetime audio", " on facetime", " on face time", " using facetime", " with facetime", " on video",
              " from my phone", " from phone", " on my phone", " on the phone", " on phone", " from the phone app", " using the phone app",
              " on the phone app", " now", " please", " for me", " right now"] {
        if let Some(i) = name.find(t) {
            name.truncate(i);
        }
    }
    let name = name.trim();
    if name.split_whitespace().count() > 4 {
        return None; // a sentence, not a name — let the model read it
    }
    // keep the user's capitals ("Vijval", not "vijval")
    let shown = raw.to_lowercase().find(name).filter(|_| !name.is_empty()).map(|i| raw[i..i + name.len()].to_string()).unwrap_or_else(|| name.to_string());
    Some(Reflex::tool("call_contact", json!({"name": shown, "via": via}), ""))
}

/// "how many PNG files are on my desktop" / "list me all the PDFs in my
/// downloads" → (extension, folder).  Counted and listed without the model
/// (live log: the model took 13.5 s for this and said 5 when there were 7).
fn list_files_request(s: &str) -> Option<(String, String, bool)> {
    let folder = folder_of(s)?;
    let counting = has_word(s, "how many") || has_word(s, "number of") || s.starts_with("count ");
    let listing = ["list", "show", "tell me", "what", "which", "give me", "find all", "find every"].iter().any(|v| s.starts_with(v));
    if !(counting || listing) {
        return None;
    }
    // "…and open the newest" / "…then delete them" is more than a list
    let full = format!(" {s} ");
    if [" and open", " and delete", " and send", " and move", " and copy", " then ", " and email"].iter().any(|p| full.contains(p)) {
        return None;
    }
    const EXT: &[(&str, &str)] = &[
        ("png", "png"), ("pngs", "png"), ("jpg", "jpg"), ("jpgs", "jpg"), ("jpeg", "jpeg"), ("jpegs", "jpeg"), ("pdf", "pdf"),
        ("pdfs", "pdf"), ("docx", "docx"), ("pptx", "pptx"), ("xlsx", "xlsx"), ("csv", "csv"), ("csvs", "csv"), ("txt", "txt"),
        ("mp3", "mp3"), ("mp3s", "mp3"), ("mp4", "mp4"), ("mp4s", "mp4"), ("mov", "mov"), ("zip", "zip"), ("zips", "zip"),
        ("heic", "heic"), ("gif", "gif"), ("gifs", "gif"), ("svg", "svg"), ("dmg", "dmg"), ("dmgs", "dmg"),
    ];
    let words: Vec<&str> = s.split_whitespace().collect();
    for (i, w) in words.iter().enumerate() {
        if let Some((_, e)) = EXT.iter().find(|(k, _)| k == w) {
            // "png files" / "pngs" — a type, not part of a name
            let plural = w.ends_with('s') && !["mov", "heic", "svg"].contains(e);
            if plural || words.get(i + 1).is_some_and(|n| ["file", "files", "images", "photos"].contains(n)) {
                return Some((e.to_string(), folder, counting));
            }
        }
    }
    // no type: "how many files are on my desktop"
    has_word(s, "files").then(|| (String::new(), folder, counting))
}

/// "notes.txt from my desktop" / "my resume" → (name, folder), if it names a file.
fn file_target(rest_simple: &str, raw_rest: &str) -> Option<(String, Option<String>)> {
    let folder = folder_of(rest_simple);
    // a name with an extension, from the raw words ("notes.txt")
    let ext = raw_rest.split_whitespace().map(|w| w.trim_matches(|c: char| ",!?\"'".contains(c))).find(|w| {
        w.rsplit_once('.').is_some_and(|(a, b)| !a.is_empty() && (2..=5).contains(&b.len()) && b.chars().all(|c| c.is_ascii_alphanumeric()) && !["com", "org", "net", "io", "in", "edu", "app"].contains(&b))
    });
    if let Some(n) = ext {
        return Some((n.to_string(), folder));
    }
    let mut cut = rest_simple;
    for sep in [" from ", " in my ", " in the ", " in ", " on my ", " on the "] {
        cut = cut.split(sep).next().unwrap_or(cut);
    }
    let words: Vec<&str> = cut.split_whitespace().filter(|w| !["my", "the", "a", "that", "this", "latest", "file", "called", "named"].contains(w)).collect();
    if words.is_empty() || words.len() > 4 || !cut.split_whitespace().any(|w| FILE_WORDS.contains(&w)) {
        return None;
    }
    // "any random file" / "one of the files" names no file
    const VAGUE: &[&str] = &["any", "random", "some", "one", "another", "whichever", "of", "them", "those", "these", "files"];
    if words.iter().all(|w| VAGUE.contains(w)) {
        return None;
    }
    Some((words.join(" "), folder))
}

const FILLERS: &[&str] = &[
    "please", "can you", "could you", "would you", "will you", "hey", "demascas", "damascus", "okay", "ok",
    "now", "just", "for me", "quickly", "go ahead and", "i want you to", "i want to", "id like to",
    "alright", "all right",
];

/// Lowercase, strip polite fillers at the edges, keep apostrophe-free words.
fn core(text: &str) -> String {
    let mut s = format!(" {} ", simplify(text));
    for f in FILLERS {
        s = s.replace(&format!(" {f} "), " ");
    }
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// A plausible app name: 1–3 words, no verbs or joiners that mean "more to do".
fn app_name(words: &str) -> Option<String> {
    let w: Vec<&str> = words.split_whitespace().filter(|w| !["the", "app", "application", "my"].contains(w)).collect();
    if w.is_empty() || w.len() > 3 {
        return None;
    }
    const NOT_APP: &[&str] = &[
        "and", "then", "to", "in", "on", "with", "for", "a", "an", "it", "that", "this", "file", "folder",
        "tab", "window", "page", "link", "url", "website", "site", "document", "presentation", "email",
        "message", "chat", "section", "settings", "downloads", "documents", "desktop", "music", "video",
        "song", "everything", "all",
    ];
    if w.iter().any(|x| NOT_APP.contains(x) || x.contains('.') || x.chars().all(|c| c.is_ascii_digit())) {
        return None;
    }
    Some(w.iter().map(|x| {
        let mut c = x.chars();
        c.next().map(|f| f.to_uppercase().collect::<String>() + c.as_str()).unwrap_or_default()
    }).collect::<Vec<_>>().join(" "))
}

/// The app a request names: checked against the real app list when known.
fn app(words: &str, recent: &Recent) -> Option<String> {
    match app_name(words) {
        Some(guess) if recent.apps.is_empty() => Some(guess),
        Some(guess) => resolve_app(&guess, &recent.apps).or_else(|| resolve_app(words, &recent.apps)),
        // "this card" fails the plain-name check ("this") but SOUNDS like Discord
        None if !recent.apps.is_empty() && words.split_whitespace().count() <= 3 => {
            let sk = crate::normalize::skeleton(words);
            (sk.len() >= 4).then(|| recent.apps.iter().find(|a| crate::normalize::skeleton(a) == sk).cloned()).flatten()
        }
        None => None,
    }
}

fn after<'a>(s: &'a str, prefixes: &[&str]) -> Option<&'a str> {
    prefixes.iter().find_map(|p| s.strip_prefix(p).map(str::trim))
}

/// The reflex for this request, if it is one.  `now` is injected for tests.
pub fn reflex(text: &str, recent: &Recent, now: chrono_lite::Now) -> Option<Reflex> {
    let raw = text.trim();
    let s = core(raw);
    if s.is_empty() {
        return None;
    }
    // "open, porn hub" / "open, burn, copy," — a verb cut off by a comma is
    // recogniser debris, not a command (live session log)
    let fragment = raw.split_whitespace().next().is_some_and(|w| w.ends_with(','));

    // ── standing permissions and "what did you do?"
    if let Some(r) = governance_reflex(&s) {
        return Some(r);
    }

    // ── timers, alarms, reminders (kept by DEMASCAS itself)
    if let Some(r) = events::parse(raw, &recent.clock) {
        return Some(Reflex::Event(r));
    }
    // "open youtube.com" is a web address, not an app (the simplifier drops dots)
    let has_address = raw.split_whitespace().any(|w| {
        let w = w.trim_end_matches(['.', ',', '!', '?']);
        w.contains('.') || w.starts_with("http") || w.starts_with("www")
    });

    // ── time / date (local clock — never a tool, never the model)
    const TIME_Q: &[&str] = &["what time is it", "whats the time", "what is the time", "tell me the time", "time please", "current time",
        "what is the current time", "whats the current time", "tell me the current time", "what time is it now", "what is the time now",
        "whats the time now", "do you know what time it is", "can you tell me the time", "what time it is"];
    if TIME_Q.iter().any(|q| s == *q || s.starts_with(&format!("{q} "))) && !s.contains(" in ") {
        return Some(Reflex::Say(format!("It's {}.", now.time_spoken())));
    }
    const DATE_Q: &[&str] = &["whats todays date", "what is todays date", "what is the date", "whats the date", "what day is it", "what day is it today", "todays date", "what is the date today", "whats the date today"];
    if DATE_Q.iter().any(|q| s == *q) {
        return Some(Reflex::Say(format!("Today is {}.", now.date_spoken())));
    }

    // ── weather
    if let Some(rest) = after(&s, &["whats the weather", "what is the weather", "how is the weather", "hows the weather", "weather", "whats the weather like", "what is the weather like",
        "look up the weather", "check the weather", "get the weather", "get me the weather", "tell me the weather", "show me the weather"]) {
        let rest = rest.strip_prefix("like").unwrap_or(rest).trim();
        let rest = rest.strip_suffix(" today").or_else(|| rest.strip_suffix(" now")).unwrap_or(rest);
        let loc = if ["", "today", "now", "outside"].contains(&rest) {
            Some("")
        } else {
            ["in ", "at ", "for "].iter().find_map(|p| rest.strip_prefix(p))
        };
        // "weather forecast for the week" etc. → not a reflex (the model handles it)
        if let Some(loc) = loc.filter(|l| l.split_whitespace().count() <= 3 && !l.contains("tomorrow") && !l.contains("week")) {
            return Some(Reflex::tool("get_weather", json!({"location": title(loc)}), ""));
        }
    }
    if recent.last_tool.as_deref() == Some("get_weather") {
        if let Some(loc) = after(&s, &["and in", "what about", "how about", "and for", "and"]) {
            let loc = loc.strip_prefix("in ").unwrap_or(loc);
            if !loc.is_empty() && loc.split_whitespace().count() <= 3 {
                return Some(Reflex::tool("get_weather", json!({"location": title(loc)}), ""));
            }
        }
    }

    // ── remember …
    if let Some(fact) = after(&s, &["remember that", "remember"]) {
        let original = strip_lead(raw, &["remember that", "remember"]);
        if fact.split_whitespace().count() >= 3 && !fact.starts_with("when") && !fact.starts_with("what") {
            return Some(Reflex::tool("save_memory", json!({"text": original}), "Got it, I'll remember that."));
        }
    }

    // ── learn an app
    if let Some(app) = after(&s, &["learn how to use", "learn to use", "learn how", "learn"]) {
        if let Some(app) = app_name(app) {
            return Some(Reflex::tool("learn_app", json!({"app_name": app}), ""));
        }
    }

    // ── "make a presentation about black holes" — one tool does research + slides
    if !fragment {
        for verb in ["make ", "create ", "build ", "prepare ", "do ", "generate "] {
            for kind in ["a presentation", "a powerpoint", "slides", "a slide deck", "a deck", "a ppt", "presentation"] {
                for prep in [" about ", " on ", " for "] {
                    let pre = format!("{verb}{kind}{prep}");
                    if let Some(topic) = s.strip_prefix(&pre) {
                        let topic = topic.trim().trim_start_matches("the ").trim();
                        if !topic.is_empty() && !["it", "them", "this", "that"].contains(&topic) {
                            let topic = if topic == "yourself" || topic == "you" { "DEMASCAS" } else { topic };
                            return Some(Reflex::tool("create_presentation", json!({"topic": topic}), ""));
                        }
                    }
                }
            }
        }
    }

    // ── websites, YouTube, email drafts, clipboard, messages — known recipes
    if !fragment {
        if let Some(r) = web_reflex(raw, &s) {
            return Some(r);
        }
        if let Some(r) = message_reflex(raw, &s) {
            return Some(r);
        }
    }

    // ── "summarise the latest PDF", "research black holes"
    if ["summarise the latest pdf", "summarize the latest pdf", "summarise my latest pdf", "summarize my latest pdf",
        "summarise the last pdf", "summarize the last pdf", "summarise the newest pdf", "summarize the newest pdf"].iter().any(|p| s.starts_with(p)) {
        return Some(Reflex::tool("summarize_latest_pdf", json!({}), ""));
    }
    // "compare the iPhone 16 and the Pixel 9" — one research job
    if let Some(rest) = s.strip_prefix("compare ") {
        let rest = rest.trim_end_matches(" for me").trim();
        for sep in [" and ", " with ", " vs ", " versus "] {
            if let Some((a, b)) = rest.split_once(sep) {
                let (a, b) = (a.trim_start_matches("the ").trim(), b.trim_start_matches("the ").trim());
                if !a.is_empty() && !b.is_empty() {
                    return Some(Reflex::tool("research_web", json!({"topic": format!("{a} vs {b} comparison")}), ""));
                }
            }
        }
    }
    // "save the summary as a note" / "save that to my notes" — the text comes from context
    for pre in ["save the summary as a note", "save it as a note", "save that as a note", "save this as a note", "save that to my notes", "save it to my notes"] {
        if s.starts_with(pre) {
            return Some(Reflex::tool("add_note", json!({"text": "", "title": "Summary"}), ""));
        }
    }
    for pre in ["research ", "read up on ", "look into ", "find out about "] {
        if let Some(topic) = s.strip_prefix(pre) {
            let topic = topic.trim_end_matches(" for me").trim_start_matches("about ").trim();
            if topic.split_whitespace().count() >= 1 && topic.split_whitespace().count() <= 8 && !topic.contains(" and ") {
                return Some(Reflex::tool("research_web", json!({"topic": topic}), ""));
            }
        }
    }

    // ── the morning brief
    const BRIEF: &[&str] = &["brief me", "morning brief", "give me my briefing", "give me my brief", "daily brief", "my briefing",
        "whats my day like", "what does my day look like", "how does my day look", "what is my day like", "whats happening today",
        "get me ready for the day", "get me ready for today", "prepare me for the day", "start my day", "lets start the day"];
    if BRIEF.iter().any(|p| s.starts_with(p)) {
        return Some(Reflex::tool("morning_brief", json!({}), ""));
    }

    // ── reading: the battery, the screen, a document
    const BATTERY: &[&str] = &["how much battery do i have", "how much battery is left", "whats my battery", "what is my battery",
        "battery level", "battery percentage", "whats the battery", "how is my battery", "check my battery", "check the battery"];
    if BATTERY.iter().any(|p| s.starts_with(p)) {
        return Some(Reflex::tool("get_system_info", json!({}), ""));
    }
    const SCREEN: &[&str] = &["read this page", "read the page", "read this to me", "read the screen", "read my screen", "what does this say",
        "what does this page say", "whats on this page", "what is on this page", "summarise this page", "summarize this page",
        "summarise this", "summarize this", "explain this page"];
    if SCREEN.iter().any(|p| s.starts_with(p)) {
        return Some(Reflex::tool("read_screen_text", json!({}), ""));
    }
    // seeing, not reading: the vision model looks at the window (loaded on demand)
    const SEE: &[&str] = &["whats on my screen", "what is on my screen", "what am i looking at", "what do you see", "describe my screen",
        "describe the screen", "describe what", "look at my screen", "look at the screen", "can you see my screen", "what is this", "whats this"];
    if SEE.iter().any(|p| s.starts_with(p)) && !s.contains("file") {
        return Some(Reflex::tool("describe_screen", json!({}), ""));
    }
    // "tell me what is currently being displayed on the screen", "what do you see on my screen"
    {
        let w: Vec<&str> = s.split_whitespace().collect();
        let screen = w.iter().any(|x| *x == "screen" || *x == "display");
        let asks = w.iter().any(|x| ["what", "whats", "tell", "read", "describe", "displayed", "showing", "shown", "see", "explain", "summarise", "summarize"].contains(x));
        let other = w.iter().any(|x| ["screenshot", "record", "recording", "share", "sharing", "brightness", "lock", "saver", "resolution", "time", "mirror", "off", "dim"].contains(x));
        if screen && asks && !other && w.len() <= 14 {
            let reading = w.first().is_some_and(|f| *f == "read") || s.contains("text") || s.contains("say");
            return Some(Reflex::tool(if reading { "read_screen_text" } else { "describe_screen" }, json!({}), ""));
        }
    }
    // "how is the internet connection?", "am I online", "is the wifi working"
    {
        let w: Vec<&str> = s.split_whitespace().collect();
        let net = w.iter().any(|x| ["internet", "wifi", "online", "network", "connection", "connected"].contains(x)) || s.contains("wi fi");
        let check = w.first().is_some_and(|f| ["how", "hows", "is", "am", "are", "check", "test", "whats", "what", "do", "does"].contains(f));
        let not_net = ["search", "explorer", "look up", "google", "find", "browse"].iter().any(|x| s.contains(x));
        if net && check && !not_net && w.len() <= 12 && (s.contains("internet") || s.contains("wifi") || s.contains("wi fi") || s.contains("online") || s.contains("network")) {
            return Some(Reflex::tool("check_internet", json!({}), ""));
        }
    }
    // "list all the files you see in the Finder app", "what's in this Finder window"
    if (s.starts_with("list") || s.starts_with("what files") || s.starts_with("whats in")) && s.contains("finder") {
        return Some(Reflex::tool("read_screen_text", json!({"app_name": "Finder"}), ""));
    }
    if let Some(f) = &recent.last_file {
        let refers = ["summarise", "summarize", "what does", "read", "give me a summary of", "tell me what"].iter().any(|v| s.starts_with(v))
            && (s.contains(" it") || s.contains(" that") || s.contains(" the file") || s.contains(" this file") || s.contains(" report")
                || s.contains(" document") || s.contains(" pdf") || s.contains(" the "));
        if refers {
            return Some(Reflex::tool("read_file_text", json!({"file_path": f}), ""));
        }
    }

    // ── Calendar, Reminders, Contacts, Mail, Notes (the Apple apps on this Mac)
    if !fragment {
        if let Some(r) = apple_reflex(raw, &s) {
            return Some(r);
        }
    }

    // ── "check what messages Krishmithal sent me on Discord" — read that chat
    if let Some((app, contact)) = read_messages_request(raw) {
        return Some(Reflex::tool("read_chat_messages", json!({"app": app, "contact": contact}), ""));
    }

    // ── "show it to me" / "open it" — the file we were just talking about
    if let Some(f) = &recent.last_file {
        const IT: &[&str] = &["show it to me", "show it", "show me", "open it", "open that", "open that file", "open the file",
            "show me the file", "show me that", "show that", "open this file", "show me it", "let me see it", "show me that file"];
        // "open the presentation" right after making one → that file, not a search
        let ext = f.rsplit('.').next().unwrap_or("").to_lowercase();
        let kind_matches = |kind: &str| match kind {
            "presentation" | "slides" | "deck" | "powerpoint" => ["pptx", "ppt", "key"].contains(&ext.as_str()),
            "document" | "doc" | "letter" | "report" | "essay" => ["docx", "doc", "pages", "pdf", "md", "txt"].contains(&ext.as_str()),
            "spreadsheet" | "sheet" | "excel" | "timetable" => ["xlsx", "xls", "numbers", "csv"].contains(&ext.as_str()),
            _ => false,
        };
        let named_kind = |t: &str| {
            let w: Vec<&str> = t.split_whitespace().collect();
            matches!(w.first(), Some(&"open") | Some(&"show"))
                && (2..=5).contains(&w.len())
                && w.last().is_some_and(|k| kind_matches(k))
                && w[1..w.len() - 1].iter().all(|x| ["me", "the", "my", "that", "this", "new"].contains(x))
        };
        let head = s.strip_suffix(" and show it to me").or_else(|| s.strip_suffix(" and show it")).or_else(|| s.strip_suffix(" and show me"));
        let tail_ok = head.is_some_and(|h| IT.contains(&h) || named_kind(h));
        if IT.contains(&s.as_str()) || named_kind(&s) || tail_ok {
            return Some(Reflex::tool("open_file", json!({"file_path": f}), format!("Opening {}.", f.rsplit('/').next().unwrap_or(f))));
        }
    }

    // ── "what / how many apps are open right now" — from the running apps, not the web (live 18 Nov)
    let apps_word = ["apps", "applications", "programs", "app", "windows"].iter().any(|w| has_word(&s, w));
    if apps_word && (has_word(&s, "open") || has_word(&s, "running")) && !s.starts_with("open ")
        && ["what", "which", "list", "show", "tell", "whats", "name", "how many", "count"].iter().any(|v| s.starts_with(v)) {
        return Some(Reflex::tool("list_running_applications", json!({}), ""));
    }
    // ── "interact with WhatsApp" / "use Discord" / "work with Excel" — open it, then ask
    //    what to do in it (live 18 Nov: refused, then "what would you like me to do?")
    for v in ["interact with ", "use the ", "use ", "work with ", "work in ", "control ", "operate ", "access ", "go into ", "get into "] {
        if let Some(rest) = s.strip_prefix(v).or_else(|| s.find(&format!(" {v}")).map(|i| &s[i + v.len() + 1..])) {
            let rest = rest.trim().trim_end_matches(" application").trim_end_matches(" app").trim();
            if rest.split_whitespace().count() <= 3 && !rest.is_empty() {
                if let Some(app) = app(rest, recent) {
                    return Some(Reflex::tool("open_application", json!({"app_name": app}), format!("{app} is open — what would you like me to do in it?")));
                }
            }
        }
    }
    // ── "check my WhatsApp messages" / "any new messages on Discord" — nobody named:
    //    open the app and read the chat list (live 18 Nov: refused as "not allowed")
    if let Some(app) = CHAT_APPS.iter().find(|a| has_word(&s, a) && **a != "messages") {
        let about_msgs = ["message", "messages", "chats", "chat", "texts", "dms", "unread"].iter().any(|w| has_word(&s, w));
        let reading = ["check", "read", "any", "show", "what", "whats", "do i have", "did i get", "see", "look", "open"].iter().any(|w| has_word(&s, w));
        let sending = ["send", "reply", "type", "write", "tell", "message to", "text to"].iter().any(|w| has_word(&s, w));
        if about_msgs && reading && !sending && read_messages_request(&s).is_none() {
            let name = match *app { "whatsapp" => "WhatsApp".to_string(), a => { let mut c = a.chars(); c.next().map(|f| f.to_uppercase().collect::<String>() + c.as_str()).unwrap_or_default() } };
            return Some(Reflex::tool("read_chat_messages", json!({"app": name, "contact": ""}), ""));
        }
    }
    // ── the clipboard: "what did I copy", "what's in my clipboard"
    let clip = has_word(&s, "clipboard") || has_word(&s, "copied") || has_word(&s, "i copied");
    if clip && has_word(&s, "open") {
        // "open the file path I copied" / "open that path from the clipboard"
        return Some(Reflex::tool("open_copied_path", json!({}), ""));
    }
    if clip && ["what", "whats", "tell", "read", "show", "say", "last thing", "check"].iter().any(|w| has_word(&s, w)) {
        return Some(Reflex::tool("get_clipboard", json!({}), ""));
    }
    // ── "open the screen recording settings" / "open the permission settings"
    if has_word(&s, "open") && ["settings", "permission", "permissions", "option", "preferences"].iter().any(|w| has_word(&s, w)) {
        for (k, pane) in [("screen recording", "screen recording"), ("screen", "screen recording"), ("accessibility", "accessibility"),
                          ("microphone", "microphone"), ("mic", "microphone"), ("full disk", "full disk access"), ("automation", "automation"),
                          ("contacts", "contacts"), ("permission", "privacy"), ("privacy", "privacy")] {
            if has_word(&s, k) {
                return Some(Reflex::tool("open_settings_pane", json!({"pane": pane}), ""));
            }
        }
    }
    // ── "open the Applications folder" — a place, not a keyboard shortcut (live: ⌘⇧U, failed)
    if let Some(rest) = s.strip_prefix("open ").or_else(|| s.strip_prefix("show me ")).or_else(|| s.strip_prefix("go to ")) {
        let r = rest.trim_start_matches("the ").trim_start_matches("my ");
        for (w, path) in [("applications", "/Applications"), ("apps", "/Applications"), ("utilities", "/Applications/Utilities"),
                          ("downloads", "~/Downloads"), ("documents", "~/Documents"), ("desktop", "~/Desktop"), ("pictures", "~/Pictures"),
                          ("music", "~/Music"), ("movies", "~/Movies"), ("home", "~")] {
            if r == format!("{w} folder") || r == w && w != "apps" && w != "home" || r == format!("{w} directory") {
                return Some(Reflex::tool("open_file", json!({"file_path": path}), format!("Opening {}.", if w == "home" { "your home folder".to_string() } else { format!("{}", w[..1].to_uppercase() + &w[1..]) })));
            }
        }
    }
    // ── "open any random PNG file from my pictures" / "a random presentation on this laptop"
    if let Some(r) = random_file(&s) {
        return Some(r);
    }

    // ── "what is two plus two times three" — exact, no model (Gemma said 7)
    if let Some(v) = spoken_math(&s) {
        return Some(Reflex::Say(format!("It's {v}.")));
    }

    // ── "open the image that looks like a QR code from my pictures" — looks INSIDE the images
    if let Some(r) = image_by_content(&s) {
        return Some(r);
    }
    // ── "what does this form ask" / "read this form"
    if ["read this form", "read the form", "what does this form ask", "what does the form ask", "what are the questions",
        "what questions are on", "read the questions", "read me the questions", "what is this form", "whats this form"].iter().any(|p| s.starts_with(p)) {
        return Some(Reflex::tool("form_read", json!({}), ""));
    }

    // ── "how many PNG files are on my desktop" / "list the PDFs in downloads"
    if let Some((ext, folder, counting)) = list_files_request(&s) {
        let action = if counting { FileAction::Count } else { FileAction::List };
        return Some(Reflex::File { action, name: ext, folder: Some(folder) });
    }

    // ── files: "open my resume", "delete notes.txt from my desktop"
    let raw_lower = raw.to_lowercase();
    for (verbs, action) in [
        (&["delete", "remove", "trash"][..], FileAction::Delete),
        (&["open"][..], FileAction::Open),
        (&["find", "where is", "wheres", "locate", "search for"][..], FileAction::Find),
        (&["summarise", "summarize", "read me", "give me a summary of"][..], FileAction::Read),
    ] {
        if let Some(rest) = after(&s, verbs).filter(|_| !fragment && (!has_address || action == FileAction::Delete)) {
            // "find my resume and open it" is two steps — leave it to the loop
            if action == FileAction::Find && (rest.contains(" and ") || rest.contains(" then ")) {
                continue;
            }
            let raw_rest = verbs.iter().find_map(|v| raw_lower.find(v).map(|i| &raw[i + v.len()..])).unwrap_or("");
            if action != FileAction::Open || app(rest, recent).is_none() {
                if let Some((name, folder)) = file_target(rest, raw_rest) {
                    return Some(Reflex::File { action, name, folder });
                }
            }
        }
    }

    // ── open / close / switch apps (real apps only)
    if let Some(rest) = after(&s, &["open up", "open", "launch", "start"]).filter(|_| !has_address && !fragment) {
        if let Some(app) = app(rest, recent) {
            return Some(Reflex::tool("open_application", json!({"app_name": app}), format!("{app} is open.")));
        }
    }
    if let Some(rest) = after(&s, &["switch to", "go to", "bring up", "show me"]).filter(|_| !has_address && !fragment) {
        if let Some(app) = app(rest, recent) {
            return Some(Reflex::tool("activate_application", json!({"app_name": app}), ""));
        }
    }
    if let Some(rest) = after(&s, &["close", "quit", "exit"]).filter(|_| !fragment) {
        let target = if ["it", "that", "that app", "this app", "this", "the app"].contains(&rest) { recent.last_app.clone() } else { app(rest, recent) };
        if let Some(app) = target {
            return Some(Reflex::tool("quit_application", json!({"app_name": app}), format!("Closed {app}.")));
        }
    }

    // ── keyboard shortcuts
    shortcuts::lookup(raw).map(|keys| Reflex::tool("press_keyboard_shortcut", json!({"keys": keys}), "Done."))
}

fn strip_lead(raw: &str, leads: &[&str]) -> String {
    let lower = raw.to_lowercase();
    for l in leads {
        if let Some(i) = lower.find(l) {
            return raw[i + l.len()..].trim().trim_end_matches(['.', '!']).to_string();
        }
    }
    raw.to_string()
}

fn title(s: &str) -> String {
    s.split_whitespace()
        .map(|w| {
            let mut c = w.chars();
            c.next().map(|f| f.to_uppercase().collect::<String>() + c.as_str()).unwrap_or_default()
        })
        .collect::<Vec<_>>()
        .join(" ")
}

/// Tiny local clock, so the crate needs no date library.
pub mod chrono_lite {
    #[derive(Debug, Clone, Copy)]
    pub struct Now {
        pub hour: u32,
        pub minute: u32,
        /// 0 = Sunday
        pub weekday: u32,
        pub day: u32,
        pub month: u32,
        pub year: i32,
    }

    const MONTHS: [&str; 12] = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
    const DAYS: [&str; 7] = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

    impl Now {
        /// The local time, read from the OS (`date`), falling back to UTC.
        pub fn local() -> Self {
            if let Ok(out) = std::process::Command::new("date").arg("+%H %M %w %d %m %Y").output() {
                let v: Vec<i64> = String::from_utf8_lossy(&out.stdout).split_whitespace().filter_map(|x| x.parse().ok()).collect();
                if v.len() == 6 {
                    return Now { hour: v[0] as u32, minute: v[1] as u32, weekday: v[2] as u32, day: v[3] as u32, month: v[4] as u32, year: v[5] as i32 };
                }
            }
            let secs = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0);
            from_unix(secs)
        }

        pub fn time_spoken(&self) -> String {
            let (h, ampm) = match self.hour { 0 => (12, "AM"), 1..=11 => (self.hour, "AM"), 12 => (12, "PM"), _ => (self.hour - 12, "PM") };
            format!("{h}:{:02} {ampm}", self.minute)
        }

        pub fn date_spoken(&self) -> String {
            format!("{}, {} {}, {}", DAYS[(self.weekday % 7) as usize], MONTHS[((self.month + 11) % 12) as usize], self.day, self.year)
        }
    }

    /// Civil date from a Unix time (UTC).
    pub fn from_unix(secs: i64) -> Now {
        let days = secs.div_euclid(86_400);
        let rem = secs.rem_euclid(86_400);
        let z = days + 719_468;
        let era = z.div_euclid(146_097);
        let doe = z - era * 146_097;
        let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
        let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
        let mp = (5 * doy + 2) / 153;
        let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
        let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
        let y = (yoe + era * 400 + if m <= 2 { 1 } else { 0 }) as i32;
        Now { hour: (rem / 3600) as u32, minute: ((rem % 3600) / 60) as u32, weekday: ((days + 4).rem_euclid(7)) as u32, day: d, month: m, year: y }
    }
}

#[cfg(test)]
mod tests {
    use super::chrono_lite::{from_unix, Now};
    use super::*;

    fn now() -> Now {
        Now { hour: 15, minute: 7, weekday: 1, day: 16, month: 11, year: 2026 }
    }

    fn tool(r: Option<Reflex>) -> (String, Value) {
        match r {
            Some(Reflex::Tool { name, args, .. }) => (name.to_string(), args),
            other => panic!("not a tool reflex: {other:?}"),
        }
    }

    #[test]
    fn interact_opens_the_app() {
        let r = Recent::default();
        assert_eq!(reflex("I want you to interact with WhatsApp application.", &r, now()),
            Some(Reflex::tool("open_application", json!({"app_name": "Whatsapp"}), "Whatsapp is open — what would you like me to do in it?")));
        assert!(!matches!(reflex("use a different word", &r, now()), Some(Reflex::Tool { name: "open_application", .. })));
    }

    #[test]
    fn check_chat_app_without_a_name() {
        let r = Recent::default();
        assert_eq!(reflex("Check for WhatsApp messages.", &r, now()), Some(Reflex::tool("read_chat_messages", json!({"app": "WhatsApp", "contact": ""}), "")));
        assert_eq!(reflex("any new messages on Discord?", &r, now()), Some(Reflex::tool("read_chat_messages", json!({"app": "Discord", "contact": ""}), "")));
        assert!(!matches!(reflex("send hi to Aman on WhatsApp", &r, now()), Some(Reflex::Tool { name: "read_chat_messages", .. })));
    }

    #[test]
    fn live_log_18_nov_app_session() {
        let r = Recent::default();
        assert_eq!(reflex("how many apps are open right now?", &r, now()), Some(Reflex::tool("list_running_applications", json!({}), "")));
        assert_eq!(reflex("tell me what I have currently copied in the clipboard.", &r, now()), Some(Reflex::tool("get_clipboard", json!({}), "")));
        assert_eq!(reflex("just tell me what is the last thing which I copied to the clipboard.", &r, now()), Some(Reflex::tool("get_clipboard", json!({}), "")));
        assert_eq!(reflex("I have copied a file path in the clipboard, I want you to open that file path and tell me what it is.", &r, now()), Some(Reflex::tool("open_copied_path", json!({}), "")));
        assert_eq!(reflex("open the permission settings so that I can enable your screen recording option.", &r, now()), Some(Reflex::tool("open_settings_pane", json!({"pane": "screen recording"}), "")));
        assert!(matches!(reflex("open the Applications folder.", &r, now()), Some(Reflex::Tool { name: "open_file", .. })));
        // "open Safari" is still an app
        assert!(matches!(reflex("open Safari", &r, now()), Some(Reflex::Tool { name: "open_application", .. })));
    }

    #[test]
    fn random_files_and_apps() {
        let r = Recent::default();
        assert_eq!(reflex("open any random PNG file from the pictures folder.", &r, now()),
            Some(Reflex::File { action: FileAction::Random, name: "png".into(), folder: Some("~/Pictures".into()) }));
        assert_eq!(reflex("open any random presentation which is on the slot top.", &r, now()),
            Some(Reflex::File { action: FileAction::Random, name: "pptx,key,ppt".into(), folder: None }));
        assert_eq!(reflex("take any random file, any random PNG file and open it.", &r, now()),
            Some(Reflex::File { action: FileAction::Random, name: "png".into(), folder: None }));
        assert_eq!(reflex("what are all the apps that are open right now?", &r, now()), Some(Reflex::tool("list_running_applications", json!({}), "")));
        assert_eq!(reflex("how many PNG files are in my picture folder?", &r, now()),
            Some(Reflex::File { action: FileAction::Count, name: "png".into(), folder: Some("~/Pictures".into()) }));
    }

    #[test]
    fn spoken_arithmetic() {
        let r = Recent::default();
        let say = |t: &str| reflex(t, &r, now());
        assert_eq!(say("what is two plus two times three"), Some(Reflex::Say("It's 8.".into())));
        assert_eq!(say("what is 36 minus 7"), Some(Reflex::Say("It's 29.".into())));
        assert_eq!(say("calculate twelve divided by four"), Some(Reflex::Say("It's 3.".into())));
        assert_eq!(say("what is twenty five times four"), Some(Reflex::Say("It's 100.".into())));
        assert_eq!(say("what is 7 divided by 2"), Some(Reflex::Say("It's 3.5.".into())));
        assert_eq!(say("what is the capital of France"), None);
        assert_eq!(say("what is up"), None);
    }

    #[test]
    fn images_by_content() {
        let r = Recent::default();
        assert_eq!(reflex("open an image which looks like a QR code from the pictures folder.", &r, now()),
            Some(Reflex::tool("find_image_by_content", json!({"folder": "~/Pictures", "what": "qr code", "open": true}), "")));
        assert_eq!(reflex("find the screenshot with a receipt on my desktop", &r, now()),
            Some(Reflex::tool("find_image_by_content", json!({"folder": "~/Desktop", "what": "receipt", "open": false}), "")));
        assert_eq!(reflex("what does this form ask", &r, now()), Some(Reflex::tool("form_read", json!({}), "")));
    }

    #[test]
    fn calls_live_log_18_nov() {
        let r = Recent::default();
        let call = |t: &str| reflex(&crate::normalize::normalize(t), &r, now());
        assert_eq!(call("call Vijval"), Some(Reflex::tool("call_contact", json!({"name": "Vijval", "via": "phone"}), "")));
        assert_eq!(call("now call the person whose name is spread by V-I-J-V-A-L."), Some(Reflex::tool("call_contact", json!({"name": "Vijval", "via": "phone"}), "")));
        assert_eq!(call("call which will from phone."), Some(Reflex::tool("call_contact", json!({"name": "which will", "via": "phone"}), "")));
        assert_eq!(call("FaceTime mom"), Some(Reflex::tool("call_contact", json!({"name": "mom", "via": "facetime_audio"}), "")));
        assert_eq!(call("video call Aman"), Some(Reflex::tool("call_contact", json!({"name": "Aman", "via": "facetime"}), "")));
        assert_eq!(call("can you list the first five contacts I have called?"), Some(Reflex::tool("recent_calls", json!({"count": 5, "missed_only": false}), "")));
        assert_eq!(call("who called me"), Some(Reflex::tool("recent_calls", json!({"count": 5, "missed_only": false}), "")));
        assert_eq!(call("any missed calls?"), Some(Reflex::tool("recent_calls", json!({"count": 5, "missed_only": true}), "")));
        // not calls
        assert!(!matches!(call("call me Daksh"), Some(Reflex::Tool { name: "call_contact", .. })));
        assert!(!matches!(call("call it a day"), Some(Reflex::Tool { name: "call_contact", .. })));
    }

    #[test]
    fn live_log_two() {
        let r = Recent::default();
        assert_eq!(reflex("is Discord open", &r, now()), Some(Reflex::IsRunning("discord".into())));
        assert!(!matches!(reflex("is the wifi on", &r, now()), Some(Reflex::IsRunning(_))));
        assert!(!matches!(reflex("is the bank open today", &r, now()), Some(Reflex::IsRunning(_))));
        for say in ["can you tell me what is being currently displayed on the screen?", "DEMASCAS tell me what is currently being displayed on the screen.",
                    "what do you see on my screen"] {
            assert_eq!(tool(reflex(say, &r, now())).0, "describe_screen", "{say}");
        }
        assert_eq!(tool(reflex("Can you see what is currently being displayed on the screen?", &r, now())).0, "describe_screen");
        assert_eq!(tool(reflex("read the text on my screen", &r, now())).0, "read_screen_text");
        assert!(!matches!(reflex("take a screenshot", &r, now()), Some(Reflex::Tool { name: "read_screen_text", .. })));
        for say in ["check, how is the internet connection?", "am I online", "is the wifi working", "how's my internet"] {
            assert_eq!(tool(reflex(say, &r, now())).0, "check_internet", "{say}");
        }
        assert!(!matches!(reflex("search the internet for pasta recipes", &r, now()), Some(Reflex::Tool { name: "check_internet", .. })));
        let (n, a) = tool(reflex("mailed that I have received on Gmail on Brave Browser.", &r, now()));
        assert_eq!((n.as_str(), a["source"].as_str()), ("read_mail", Some("gmail")));
        let (n, a) = tool(reflex("alright Damascus, I want you to check what are the latest emails that I have received.", &r, now()));
        assert_eq!((n.as_str(), a.get("source")), ("read_mail", None));
    }

    #[test]
    fn opens_what_was_just_made() {
        let r = Recent { last_file: Some("/Users/x/Documents/Saudi Arabia.pptx".into()), ..Default::default() };
        for say in ["open the presentation", "open the presentation and show it to me", "show me the presentation", "open it and show it to me", "open my new presentation"] {
            assert!(matches!(reflex(say, &r, now()), Some(Reflex::Tool { name: "open_file", .. })), "{say}");
        }
        assert_eq!(split_compound("open the presentation and show it to me"), vec!["open the presentation"]);
        assert_eq!(split_compound("if I have 3 boxes with 12 pens each and give away 7 pens, how many are left").len(), 1);
        // a different kind still searches
        assert!(!matches!(reflex("open the spreadsheet", &r, now()), Some(Reflex::Tool { name: "open_file", .. })));
    }

    #[test]
    fn apps() {
        let r = Recent::default();
        assert_eq!(tool(reflex("open Discord", &r, now())), ("open_application".into(), json!({"app_name": "Discord"})));
        assert_eq!(tool(reflex("please launch PowerPoint", &r, now())).1, json!({"app_name": "Powerpoint"}));
        assert_eq!(tool(reflex("open the notes app", &r, now())).1, json!({"app_name": "Notes"}));
        assert_eq!(tool(reflex("close Safari", &r, now())).0, "quit_application");
        assert_eq!(tool(reflex("switch to Mail", &r, now())).0, "activate_application");
        // not reflexes — more to do, or not an app
        for t in ["open Discord and go to the friends section", "open youtube.com", "open my downloads folder", "open a presentation", "open the", "open"] {
            assert!(!matches!(reflex(t, &r, now()), Some(Reflex::Tool { name: "open_application", .. })), "{t}");
        }
    }

    #[test]
    fn close_it_uses_the_last_app() {
        let r = Recent { last_app: Some("Safari".into()), ..Default::default() };
        assert_eq!(tool(reflex("close it", &r, now())).1, json!({"app_name": "Safari"}));
        assert!(reflex("close it", &Recent::default(), now()).is_none());
    }

    #[test]
    fn time_date_weather_memory() {
        let r = Recent::default();
        assert_eq!(reflex("what time is it", &r, now()), Some(Reflex::Say("It's 3:07 PM.".into())));
        assert_eq!(reflex("what's today's date", &r, now()), Some(Reflex::Say("Today is Monday, November 16, 2026.".into())));
        assert!(reflex("what time is it in Tokyo", &r, now()).is_none());
        assert_eq!(reflex("what is the current time?", &r, now()), Some(Reflex::Say("It's 3:07 PM.".into())));
        assert_eq!(reflex("can you tell me the time", &r, now()), Some(Reflex::Say("It's 3:07 PM.".into())));
        assert_eq!(tool(reflex("what's the weather in Delhi", &r, now())).1, json!({"location": "Delhi"}));
        assert_eq!(tool(reflex("what's the weather", &r, now())).1, json!({"location": ""}));
        let w = Recent { last_tool: Some("get_weather".into()), ..Default::default() };
        assert_eq!(tool(reflex("and in Mumbai?", &w, now())).1, json!({"location": "Mumbai"}));
        assert_eq!(tool(reflex("remember that my brother's name is Neon", &r, now())).1, json!({"text": "my brother's name is Neon"}));
        assert!(reflex("remember", &r, now()).is_none());
        assert_eq!(tool(reflex("learn how to use Notion", &r, now())).1, json!({"app_name": "Notion"}));
    }

    #[test]
    fn real_apps_files_and_fragments() {
        let apps = std::sync::Arc::new(vec!["Discord".to_string(), "Microsoft PowerPoint".to_string(), "Safari".to_string(), "Notes".to_string(), "Docker".to_string(), "Dock".to_string()]);
        let r = Recent { apps, ..Default::default() };
        assert_eq!(tool(reflex("launch PowerPoint", &r, now())).1, json!({"app_name": "Microsoft PowerPoint"}));
        assert_eq!(tool(reflex("open Dock", &r, now())).1, json!({"app_name": "Dock"}));
        assert!(reflex("open porn hub", &r, now()).is_none());          // not an installed app
        assert!(reflex("open, Discord", &r, now()).is_none());          // fragment
        assert_eq!(reflex("open my resume", &r, now()), Some(Reflex::File { action: FileAction::Open, name: "resume".into(), folder: None }));
        assert_eq!(reflex("delete notes.txt from my desktop", &r, now()), Some(Reflex::File { action: FileAction::Delete, name: "notes.txt".into(), folder: Some("~/Desktop".into()) }));
        assert_eq!(reflex("find the budget spreadsheet in downloads", &r, now()), Some(Reflex::File { action: FileAction::Find, name: "budget spreadsheet".into(), folder: Some("~/Downloads".into()) }));
        assert!(reflex("find my resume and open it", &r, now()).is_none());
        assert_eq!(reflex("summarise my CV", &r, now()), Some(Reflex::File { action: FileAction::Read, name: "cv".into(), folder: None }));
        assert!(matches!(reflex("set a timer for ten minutes", &r, now()), Some(Reflex::Event(_))));
    }

    #[test]
    fn hearing_and_context() {
        let apps = std::sync::Arc::new(vec!["Discord".to_string(), "Safari".to_string()]);
        let r = Recent { apps, last_file: Some("/Users/x/Desktop/Daksh_Rathore_CV.pdf".into()), ..Default::default() };
        assert_eq!(tool(reflex("open this card.", &r, now())).1, json!({"app_name": "Discord"}));
        assert_eq!(tool(reflex("show it to me.", &r, now())), ("open_file".into(), json!({"file_path": "/Users/x/Desktop/Daksh_Rathore_CV.pdf"})));
        assert_eq!(read_messages_request("check what messages, Krishmithal has sent me on Discord"), Some(("Discord".into(), "Krishmithal".into())));
        assert_eq!(read_messages_request("what did Aman say on Slack"), Some(("Slack".into(), "Aman".into())));
        assert_eq!(read_messages_request("read my messages from Hatim Tai"), Some(("".into(), "Hatim Tai".into())));
        assert_eq!(read_messages_request("check my email"), None);
        assert_eq!(split_compound("what time is it, and what's the weather in Delhi?"), vec!["what time is it", "what's the weather in Delhi?"]);
        assert_eq!(split_compound("open Discord and check what messages Krishmithal has sent me"), vec!["open Discord", "check what messages Krishmithal has sent me"]);
        assert_eq!(split_compound("buy salt and pepper"), vec!["buy salt and pepper"]);
    }

    #[test]
    fn known_recipes() {
        let r = Recent::default();
        assert_eq!(tool(reflex("open youtube.com", &r, now())).1, json!({"url": "https://youtube.com", "browser": ""}));
        assert_eq!(tool(reflex("open google.com in Safari", &r, now())).1, json!({"url": "https://google.com", "browser": "Safari"}));
        assert_eq!(tool(reflex("open Wikipedia on Brave browser.", &r, now())).1, json!({"url": "https://en.wikipedia.org", "browser": "Brave Browser"}));
        assert_eq!(tool(reflex("search YouTube for lofi jazz", &r, now())).1["url"], json!("https://www.youtube.com/results?search_query=lofi+jazz"));
        assert!(matches!(reflex("play some lofi music", &r, now()), Some(Reflex::Steps { .. })));
        assert_eq!(tool(reflex("dm John on Slack saying the build is green", &r, now())).0, "message_contact");
        assert_eq!(tool(reflex("send hi to Hatim on Discord", &r, now())).0, "message_contact");
        assert_eq!(tool(reflex("text mom that I'll be home soon", &r, now())).0, "message_contact");
        assert!(reflex("tell me a joke", &r, now()).is_none());
        assert_eq!(tool(reflex("write an email to john@example.com about the meeting tomorrow", &r, now())).1, json!({"recipient": "john@example.com", "subject": "the meeting tomorrow", "key_points": "the meeting tomorrow"}));
        assert_eq!(tool(reflex("copy hello world to my clipboard", &r, now())).1, json!({"text": "hello world"}));
        assert_eq!(tool(reflex("write an email to Aman telling him the demo moved to Friday", &r, now())).1["recipient"], json!("Aman"));
        assert_eq!(tool(reflex("type hello everyone and press enter", &r, now())), ("type_and_submit".into(), json!({"text": "hello everyone"})));
    }

    #[test]
    fn research_and_pdf() {
        assert_eq!(tool(reflex("research electric cars", &Recent::default(), now())), ("research_web".into(), json!({"topic": "electric cars"})));
        assert_eq!(tool(reflex("research black holes for me", &Recent::default(), now())).1, json!({"topic": "black holes"}));
        assert_eq!(tool(reflex("summarize the latest PDF", &Recent::default(), now())).0, "summarize_latest_pdf");
    }

    #[test]
    fn brief() {
        assert_eq!(tool(reflex("brief me", &Recent::default(), now())).0, "morning_brief");
        assert_eq!(tool(reflex("what's my day like", &Recent::default(), now())).0, "morning_brief");
    }

    #[test]
    fn reading() {
        let r = Recent { last_file: Some("/x/report.pdf".into()), ..Default::default() };
        assert_eq!(tool(reflex("how much battery do I have", &r, now())).0, "get_system_info");
        assert_eq!(tool(reflex("read this page to me", &r, now())).0, "read_screen_text");
        assert_eq!(tool(reflex("summarise the project report", &r, now())), ("read_file_text".into(), json!({"file_path": "/x/report.pdf"})));
        // no file known yet → find it, then read it
        assert_eq!(reflex("summarise the project report", &Recent::default(), now()), Some(Reflex::File { action: FileAction::Read, name: "project report".into(), folder: None }));
    }

    #[test]
    fn apple_apps() {
        let r = Recent::default();
        assert_eq!(tool(reflex("what's on my calendar tomorrow", &r, now())), ("list_agenda".into(), json!({"day": "tomorrow", "days": 1})));
        assert_eq!(tool(reflex("what do I have on friday", &r, now())).1, json!({"day": "friday", "days": 1}));
        assert_eq!(tool(reflex("add a meeting with Aman tomorrow at 3 pm to my calendar", &r, now())).1, json!({"title": "meeting with Aman", "when": "tomorrow at 3 pm"}));
        assert_eq!(tool(reflex("schedule a call with mom on friday at 6", &r, now())).1, json!({"title": "call with mom", "when": "on friday at 6"}));
        assert_eq!(tool(reflex("what are my reminders", &r, now())).0, "list_reminders");
        assert_eq!(tool(reflex("what's Aman's number", &r, now())).1, json!({"name": "aman"}));
        assert_eq!(tool(reflex("read my latest emails", &r, now())).0, "read_mail");
        for say in ["what are the latest emails that I received?", "can you read me the latest mail that I have received?",
                    "I want you to read the latest mail that I have received on Gmail", "show me my recent emails"] {
            assert_eq!(tool(reflex(say, &r, now())).0, "read_mail", "{say}");
        }
        for say in ["write an email to Aman about the demo", "send an email to mom", "draft a mail saying I'm late"] {
            assert!(!matches!(reflex(say, &r, now()), Some(Reflex::Tool { name: "read_mail", .. })), "{say}");
        }
        assert_eq!(tool(reflex("any mail from Aman", &r, now())).1["sender"], json!("aman"));
        assert_eq!(tool(reflex("add milk to my shopping list", &r, now())).1, json!({"text": "milk", "title": "Shopping"}));
        assert_eq!(tool(reflex("take a note: call the bank on Monday", &r, now())).1, json!({"text": "call the bank on Monday"}));
        assert_eq!(tool(reflex("what's in my shopping list", &r, now())).1, json!({"query": "shopping"}));
        assert!(reflex("who is Albert Einstein", &r, now()).is_none());
    }

    #[test]
    fn governance_phrases() {
        let r = Recent::default();
        assert_eq!(reflex("always allow opening apps", &r, now()), Some(Reflex::Permission { kind: "allow", subject: "opening apps".into() }));
        assert_eq!(reflex("never delete files without asking", &r, now()), Some(Reflex::Permission { kind: "ask", subject: "delete files".into() }));
        assert_eq!(reflex("never close my apps", &r, now()), Some(Reflex::Permission { kind: "deny", subject: "close my apps".into() }));
        assert_eq!(reflex("what did you do today?", &r, now()), Some(Reflex::ActionsToday));
        assert_eq!(reflex("what are your rules", &r, now()), Some(Reflex::Rules));
        assert!(reflex("never mind", &r, now()).is_none());
    }

    #[test]
    fn shortcuts_are_reflexes() {
        assert_eq!(tool(reflex("take a screenshot", &Recent::default(), now())).1, json!({"keys": "command+shift+3"}));
    }

    #[test]
    fn clock() {
        let n = from_unix(1_794_787_200); // 2026-11-16 00:00 UTC
        assert_eq!((n.year, n.month, n.day, n.weekday), (2026, 11, 16, 1));
    }
}
