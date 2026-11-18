//! The ONE router: what to do with something the user said.  Decided in
//! order, cheapest first; the model is only reached for real work.
//!
//!  1. recogniser junk / own echo / low confidence → ignore or "say again?"
//!  2. control while a task runs (stop, status) and dismissals
//!  3. the server's verdict: a skill, or "unclear" → ask (never guess)
//!  4. side talk after the first exchange → stay quiet unless addressed
//!  5. reflexes → one exact action, no model
//!  6. questions → answered (chat) or looked up with READ-ONLY tools
//!  7. everything else → the tool loop / an agent, with full tools
//!
//! The server's route comes from `/skill/match` (embeddings, ~3 ms), so
//! deciding costs no model call.

use bridge::SkillMatch;

use crate::reflex::{self, chrono_lite::Now, Recent, Reflex};
use crate::utterance::*;

/// What we know about the conversation when a request arrives.
#[derive(Debug, Clone, Default)]
pub struct Context {
    /// First request since the wake word.
    pub first_after_wake: bool,
    /// The last thing DEMASCAS said ended with a question.
    pub it_asked: bool,
    /// A background task is running.
    pub task_busy: bool,
    /// Recogniser confidence (0–1) and share of unsure words.
    pub confidence: f32,
    pub unsure: f32,
    pub recent: Recent,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Control {
    Cancel,
    Status,
    /// Stop everything and stand by ("that's enough for the day").
    Sleep,
    Feedback(bool),
}

#[derive(Debug, Clone, PartialEq)]
pub enum Decision {
    /// Not addressed to us / not speech — say nothing.
    Ignore(&'static str),
    Control(Control),
    /// Ask instead of guessing.
    Clarify(String),
    Reflex(Reflex),
    /// A learned / built-in procedure on the server.
    Skill,
    /// Conversation or knowledge — answer, no tools.
    Answer,
    /// A question that needs looking something up — READ-ONLY tools only,
    /// so "is Discord open?" can never open Discord.
    Lookup,
    /// Do something — the tool loop with all tools.
    Act,
    /// A multi-step domain flow (messaging, presentations, app learning).
    Agent,
    /// Several actions: plan first, then run and check each step.
    Plan,
}

const ASK_AGAIN: &str = "Sorry, I didn't catch that. Could you say it again?";

/// Starts like a question ("is…", "what…", "how many…") or ends with "?".
pub fn is_question(text: &str) -> bool {
    let s = simplify(text);
    let first = s.split(' ').next().unwrap_or("");
    const Q: &[&str] = &[
        "what", "whats", "who", "whos", "when", "where", "why", "how", "hows", "which", "is", "are", "was",
        "were", "does", "do", "did", "can", "could", "should", "would", "will", "am", "tell", "explain",
        "define", "describe",
    ];
    // "can you open…" / "could you send…" are requests, not questions
    let polite_request = ["can", "could", "would", "will"].contains(&first) && has_word(&s, "you")
        && !has_word(&s, "you tell") && !has_word(&s, "you explain");
    (Q.contains(&first) && !polite_request) || text.trim_end().ends_with('?') && !polite_request
}

/// Starts (after fillers) with something you'd tell an assistant to do.
pub fn is_imperative(text: &str) -> bool {
    let (first, _) = first_word_after_fillers(text);
    let first = first.as_str();
    const VERBS: &[&str] = &[
        "open", "close", "quit", "exit", "launch", "start", "play", "pause", "resume", "stop", "send", "message",
        "text", "email", "mail", "reply", "dm", "type", "write", "create", "make", "build", "delete", "remove",
        "move", "copy", "paste", "find", "search", "look", "google", "show", "list", "read", "tell", "give",
        "set", "remind", "add", "call", "go", "switch", "turn", "mute", "unmute", "take", "save", "learn",
        "help", "check", "scroll", "click", "press", "summarize", "summarise", "translate", "explain",
        "define", "research", "download", "upload", "share", "post", "lock", "minimize", "maximize", "hide",
        "undo", "redo", "remember", "forget", "watch", "wake", "bring", "put", "rename", "draft",
        "compose", "schedule", "cancel", "run", "install", "update", "record", "zoom", "refresh", "reload",
        "print", "select", "use", "do", "try", "fix", "change", "increase", "decrease", "raise", "lower",
        "can", "could", "would", "will", "let", "lets",
        // screen and form work (18 Nov: "tick the I agree box" went to chat)
        "tick", "untick", "uncheck", "fill", "place", "submit", "choose", "pick", "enter", "tap", "hit", "mark",
        "answer", "complete", "accept", "agree", "toggle", "sign", "upload", "attach", "dismiss", "expand",
        "interact", "operate", "control", "handle", "manage", "access", "browse", "navigate", "respond", "chat", "work",
    ];
    VERBS.contains(&first)
}

pub fn decide(text: &str, ctx: &Context, server: &SkillMatch, now: Now) -> Decision {
    let text = text.trim();
    if is_hallucination(text) {
        return Decision::Ignore("recogniser junk");
    }
    let control = is_cancel_request(text) || is_status_question(text) || is_dismiss(text);

    // 1. how sure was the recogniser?
    if !control {
        if ctx.task_busy && (ctx.confidence < 0.65 || ctx.unsure > 0.2) {
            return Decision::Ignore("unclear background speech while working");
        }
        if ctx.confidence < 0.5 || ctx.unsure >= 0.35 {
            return Decision::Clarify(ASK_AGAIN.into());
        }
    }

    // 2. control
    if ctx.task_busy {
        if is_dismiss(text) && !is_cancel_request(text) || has_word(&simplify(text), "for the day") {
            return Decision::Control(Control::Sleep);
        }
        if is_cancel_request(text) {
            return Decision::Control(Control::Cancel);
        }
        if is_status_question(text) {
            return Decision::Control(Control::Status);
        }
    } else if is_dismiss(text) {
        return Decision::Control(Control::Sleep);
    } else if ["what are you doing", "what are you doing right now", "what are you doing now", "what are you working on", "are you busy",
               "are you working on something", "what are you up to right now"].contains(&strip_fillers(&simplify(text), &["demascas", "damascus", "hey", "so", "and", "okay"]).as_str()) {
        // nothing running → "Nothing right now — I'm free." (live 18 Nov: went to the model,
        // which asked what was meant)
        return Decision::Control(Control::Status);
    }
    match feedback_polarity(text) {
        1 => return Decision::Control(Control::Feedback(true)),
        -1 => return Decision::Control(Control::Feedback(false)),
        _ => {}
    }

    // Anything that changes things needs clear speech (live log: "I am going
    // to get the message, Discord," at 0.58 confidence ran a skill).
    // …or a sentence that was cut off ("send high, do,") — never act on half of it
    // "open, porn hub" — a verb cut off by a comma is a fragment too
    // …but "open, presentation file which is on this laptop" is a whole request
    let cut_verb = text.split_whitespace().next().is_some_and(|w| w.ends_with(',') && is_imperative(w.trim_end_matches(',')))
        && text.split_whitespace().count() <= 3;
    let shaky = ctx.confidence < 0.6 || ctx.unsure >= 0.25 || crate::normalize::sounds_unfinished(text) || cut_verb;
    let clarify_shaky = || Decision::Clarify("Sorry, I didn't catch that clearly — could you say it again?".into());

    // 3. reflexes — structurally certain (a shortcut, a real app, a timer);
    //    checked before the server's one-word "unclear" rule ("undo").
    //    Ones that reach other people or delete things need clear speech.
    // (a sentence with several requests is never swallowed by the recipe its
    //  start happens to match — "check my calendar … and remind me …")
    let multi = crate::reflex::split_compound(text).len() >= 2;
    // ("take any random PNG file and open it" is ONE action, not two)
    // (a reflex whose tool already does both halves isn't split either:
    //  "open that path I copied and tell me what it is" — open_copied_path says what it is)
    if let Some(r) = reflex::reflex(text, &ctx.recent, now).filter(|r| !multi
        || matches!(r, Reflex::File { action: crate::reflex::FileAction::Random, .. })
        || matches!(r, Reflex::Tool { name: "open_copied_path" | "read_chat_messages" | "open_application", .. })) {
        return if shaky && r.is_outward() { clarify_shaky() } else { Decision::Reflex(r) };
    }

    // a single stray word that isn't a command ("Happy," — live 18 Nov got
    // "What should I do with Happy?") is background talk
    let words: Vec<String> = simplify(text).split_whitespace().map(str::to_string).collect();
    if words.len() == 1 && !ctx.first_after_wake && !ctx.it_asked && !is_imperative(text) && !is_small_talk(text)
        && !["hello", "hi", "hey", "help", "yes", "no", "thanks", "undo", "redo", "pause", "resume", "next", "back"].contains(&words[0].as_str()) {
        return Decision::Ignore("one stray word");
    }

    // 4. the server's verdict — but a long multi-step request is planned,
    //    not squeezed into a one-procedure built-in skill
    if server.matched && !(crate::planner::needs_plan(text) && text.split_whitespace().count() >= 6) {
        return if shaky { clarify_shaky() } else { Decision::Skill };
    }
    if server.route == "unclear" {
        return Decision::Clarify(if server.ask.is_empty() { ASK_AGAIN.into() } else { server.ask.clone() });
    }

    // 5. side talk: after the first exchange, speech must be addressed.  The
    //    server's route can't decide this (it sent "yeah I told him that
    //    yesterday" down the TOOL path).  Questions and commands in the
    //    follow-up window count as addressed; statements are side talk.
    let imperative = is_imperative(text);
    let addressed = asks_me(text);
    // "I want you to interact with WhatsApp" is a REQUEST, whatever its verb
    // (live 18 Nov: it became a read-only question and was refused)
    let wants = ["i want you to", "i need you to", "id like you to", "i would like you to"].iter().any(|p| has_word(&simplify(text), p));
    let imperative = imperative || wants;
    // "can you open Discord" is a request; "can you process images?" is a question
    let question = !wants && (is_question(text) || (addressed && !imperative));
    if !ctx.first_after_wake && !(question || imperative || addressed) && !ctx.it_asked && !names_demascas(text) {
        return Decision::Ignore("side talk (not addressed to DEMASCAS)");
    }

    // 6. several actions → plan first ("find my report, summarise it and email Aman")
    if !question && (crate::planner::needs_plan(text) || multi) {
        return if shaky { clarify_shaky() } else { Decision::Plan };
    }

    // 6b. conversation: statements and "tell me / explain" need no tools
    //    (live log: "I'm feeling tired today" opened apps and typed)
    // (never for reminders, timers or alarms: chat can only TALK about setting them)
    let s_low = simplify(text);
    let event_words = ["remind", "reminder", "reminders", "alarm", "timer"].iter().any(|w| has_word(&s_low, w));
    // a barely-heard sentence gets "say that again", not a made-up answer
    // (live log: "what is the visual doing?" at 0.53 → an invented screen description)
    let barely = ctx.confidence < 0.55;
    // a command about something on screen is never chat, whatever the
    // server's guess ("fill in this quiz for me" was routed to chat)
    let on_screen = imperative && SCREEN_WORDS.iter().any(|w| has_word(&s_low, w));
    if (server.route == "chat" && !on_screen || (!question && !imperative) || is_conversational(text) || is_small_talk(text)) && !(event_words && !question) {
        return if barely { Decision::Clarify(ASK_AGAIN.into()) } else { Decision::Answer };
    }
    // 7a. a question about the world, not this computer or today's news, is
    //     chat (~600 tokens) — not the 2,000-token lookup prompt (live 18 Nov:
    //     "is it really good at talking?" took 14.6 s down the lookup path)
    let capability = addressed && !imperative && ["can you", "could you", "are you", "do you", "will you", "would you"]
        .iter().any(|p| simplify(text).starts_with(p));
    if question && !is_lookup(text) && (capability || !needs_tools(text, &ctx.recent)) {
        return if barely { Decision::Clarify(ASK_AGAIN.into()) } else { Decision::Answer };
    }
    // 7. questions and look-ups only READ
    if question || is_lookup(text) {
        return if barely { Decision::Clarify(ASK_AGAIN.into()) } else { Decision::Lookup };
    }

    // 8. work
    if shaky {
        return clarify_shaky();
    }
    if server.route == "agent" {
        Decision::Agent
    } else {
        Decision::Act
    }
}

fn first_word_after_fillers(text: &str) -> (String, String) {
    let mut s = simplify(text);
    for p in ["i want you to ", "i need you to ", "id like you to ", "i would like you to ", "i want to ", "i need to "] {
        if let Some(i) = s.find(p) {
            if s[..i].split_whitespace().count() <= 3 {
                s = s[i + p.len()..].to_string();
                break;
            }
        }
    }
    let s = strip_fillers(&s, &[
        "also", "and", "then", "now", "okay", "ok", "so", "yeah", "please", "alright", "hey", "just", "well",
        "um", "uh", "demascas", "damascus", "can", "could", "would", "will", "you", "actually", "instead", "rather",
        "wait", "no", "oh",
    ]);
    (s.split(' ').next().unwrap_or("").to_string(), s)
}

/// "before the test, can you tell me how many…" — a question put to
/// DEMASCAS mid-sentence (live log: ignored as side talk because it started
/// with "before").
pub fn asks_me(text: &str) -> bool {
    let s = simplify(text);
    let s2 = strip_fillers(&s, &["okay", "ok", "so", "and", "hey", "well", "demascas", "damascus", "but", "then"]);
    // "can you process images…?", "do you remember…", "are you able to…" — said TO it
    ["can you ", "could you ", "would you ", "will you ", "do you ", "did you ", "are you ", "have you ", "can u "]
        .iter().any(|p| s2.starts_with(p))
        // "I have copied a path… I want you to open it" — addressed, wherever it says so
        || ["i want you to", "i need you to", "id like you to", "i would like you to", "please"].iter().any(|p| has_word(&s, p))
        || ["can you tell me", "could you tell me", "can you check", "could you check", "can you find", "can you show me",
            "do you know", "how many", "how much"].iter().any(|p| has_word(&s, p))
}

/// "what's up?", "how are you?" — small talk, never a tool loop (live log:
/// "what is up?" got the 2,000-token tool prompt and was queued for web research).
pub fn is_small_talk(text: &str) -> bool {
    let s = strip_fillers(&simplify(text), &["hey", "hi", "hello", "so", "yo", "demascas", "damascus", "buddy", "man", "bro", "then", "today"]);
    const TALK: &[&str] = &[
        "what is up", "whats up", "sup", "wassup", "whats new", "what is new", "how are you", "how are you doing", "how r you",
        "how is it going", "hows it going", "how have you been", "how do you do", "how is your day", "hows your day",
        "how is your day going", "hows your day going", "whats going on with you", "what are you up to", "you good",
        "are you there", "are you okay", "are you ok", "good morning", "good evening", "good afternoon", "good night",
    ];
    TALK.contains(&s.as_str())
}

/// "tick the box on this page", "fill in this quiz" — about something on screen.
pub fn mentions_screen(text: &str) -> bool {
    let s = simplify(text);
    SCREEN_WORDS.iter().any(|w| has_word(&s, w)) || has_word(&s, "on screen") || has_word(&s, "tick") || has_word(&s, "untick")
}

/// Things on a screen — an imperative naming one is screen work, not chat.
const SCREEN_WORDS: &[&str] = &[
    "page", "form", "quiz", "field", "box", "checkbox", "button", "order", "cart", "link", "assignment", "dropdown",
    "tab", "screen", "window", "website", "site", "document", "test", "question", "questions", "answer", "answers",
];

/// Words about the computer — "tell me what's on my screen" is NOT chat.
const DEVICE_WORDS: &[&str] = &[
    "screen", "file", "files", "folder", "email", "emails", "mail", "message", "messages", "app", "apps", "window",
    "tab", "tabs", "clipboard", "battery", "wifi", "volume", "download", "downloads", "desktop", "documents",
    "calendar", "weather", "news", "price", "score", "discord", "slack", "whatsapp", "safari", "chrome",
];

/// Does answering this need to look at the computer or the live web?
pub fn needs_tools(text: &str, recent: &Recent) -> bool {
    let s = simplify(text);
    const LIVE: &[&str] = &[
        "today", "tonight", "tomorrow", "yesterday", "now", "right now", "currently", "current", "latest", "recent", "news", "price",
        "stock", "score", "weather", "temperature", "who won", "this week", "this year", "trending", "my", "mine", "this", "here",
        "open", "running", "installed", "on my", "how do i", "how can i", "how to", "where is", "where do", "time", "date",
    ];
    s.split_whitespace().any(|w| DEVICE_WORDS.contains(&w) || SCREEN_WORDS.contains(&w))
        || LIVE.iter().any(|w| has_word(&s, w))
        || recent.apps.iter().any(|a| a.len() >= 3 && has_word(&s, &simplify(a)))
}

/// "tell me a joke", "explain recursion", "translate hello" — answered, no tools.
pub fn is_conversational(text: &str) -> bool {
    let (first, s) = first_word_after_fillers(text);
    ["tell", "explain", "define", "describe", "translate", "give", "say", "greet", "recommend", "suggest"].contains(&first.as_str())
        && !s.split_whitespace().any(|w| DEVICE_WORDS.contains(&w))
}

/// "find my resume", "list my downloads", "read this page", "check my email" —
/// reading only (unless it goes on to act: "find X and delete it").
pub fn is_lookup(text: &str) -> bool {
    let (first, s) = first_word_after_fillers(text);
    if !["find", "list", "read", "check", "look", "search", "show"].contains(&first.as_str()) {
        return false;
    }
    // "search YouTube for…" opens a site — that's doing, not reading
    let full = format!(" {} ", simplify(text)); // with "and" kept
    let acts_after = [" and open", " and delete", " and send", " and play", " and move", " and type", " then ", " and go"].iter().any(|p| full.contains(p));
    let site = ["youtube", "amazon", "spotify", "google", "flipkart"].iter().any(|w| has_word(&s, w));
    !acts_after && !(first == "search" && site) && !(first == "show" && !has_word(&s, "files"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn now() -> Now {
        Now { hour: 9, minute: 30, weekday: 1, day: 16, month: 11, year: 2026 }
    }
    fn ctx() -> Context {
        Context { first_after_wake: true, confidence: 1.0, ..Default::default() }
    }
    fn route(r: &str) -> SkillMatch {
        SkillMatch { route: r.into(), ..Default::default() }
    }

    #[test]
    fn junk_and_low_confidence() {
        assert!(matches!(decide("Thank you.", &ctx(), &route(""), now()), Decision::Ignore(_)));
        let low = Context { confidence: 0.4, ..ctx() };
        assert_eq!(decide("delete the file report", &low, &route("tool"), now()), Decision::Clarify(ASK_AGAIN.into()));
        // stop always works, even unclear
        assert_eq!(decide("stop", &low, &route(""), now()), Decision::Control(Control::Sleep));
    }

    #[test]
    fn app_requests_are_not_read_only() {
        let later = Context { first_after_wake: false, ..ctx() };
        for t in ["I want you to interact with WhatsApp application.", "interact with WhatsApp", "I need you to handle my Discord"] {
            let d = decide(t, &later, &route("tool"), now());
            assert!(!matches!(d, Decision::Lookup | Decision::Answer | Decision::Ignore(_)), "{t}: {d:?}");
        }
    }

    #[test]
    fn status_when_idle() {
        assert_eq!(decide("what are you doing right now?", &ctx(), &route("tool"), now()), Decision::Control(Control::Status));
        assert_ne!(decide("what are you doing tomorrow at the party", &ctx(), &route("chat"), now()), Decision::Control(Control::Status));
    }

    #[test]
    fn control_while_busy() {
        let busy = Context { task_busy: true, ..ctx() };
        assert_eq!(decide("cancel that", &busy, &route(""), now()), Decision::Control(Control::Cancel));
        assert_eq!(decide("what are you doing", &busy, &route(""), now()), Decision::Control(Control::Status));
        assert_eq!(decide("that's enough for the day, I want to stop now", &busy, &route(""), now()), Decision::Control(Control::Sleep));
    }

    #[test]
    fn unclear_asks() {
        let m = SkillMatch { route: "unclear".into(), ask: "Open what?".into(), ..Default::default() };
        assert_eq!(decide("anyways, open,", &ctx(), &m, now()), Decision::Clarify("Open what?".into()));
    }

    #[test]
    fn side_talk_is_ignored_unless_addressed() {
        let later = Context { first_after_wake: false, ..ctx() };
        assert!(matches!(decide("yeah I told him that yesterday, he never listens", &later, &route("tool"), now()), Decision::Ignore(_)));
        assert_eq!(decide("DEMASCAS, tell me another one", &later, &route("chat"), now()), Decision::Answer);
        let asked = Context { it_asked: true, ..later.clone() };
        assert_eq!(decide("the blue one", &asked, &route("chat"), now()), Decision::Answer);
        // commands still work without the name
        assert!(matches!(decide("open Safari", &later, &route("tool"), now()), Decision::Reflex(_)));
        assert_eq!(decide("what's the capital of Japan", &later, &route("chat"), now()), Decision::Answer);
    }

    #[test]
    fn statements_and_lookups() {
        assert_eq!(decide("I'm feeling tired today", &ctx(), &route("tool"), now()), Decision::Answer);
        assert_eq!(decide("DEMASCAS, tell me another one", &ctx(), &route("tool"), now()), Decision::Answer);
        assert!(matches!(decide("tell me what's on my screen", &ctx(), &route("tool"), now()), Decision::Reflex(Reflex::Tool { name: "describe_screen", .. })));
        assert!(matches!(decide("find my resume", &ctx(), &route("tool"), now()), Decision::Reflex(Reflex::File { .. })));
        assert_eq!(decide("find out who won the match", &ctx(), &route("tool"), now()), Decision::Lookup);
        // listing a folder is one read — no model (18 Nov)
        assert!(matches!(decide("list the files in my downloads folder", &ctx(), &route("tool"), now()), Decision::Reflex(Reflex::File { action: crate::reflex::FileAction::List, .. })));
        assert!(matches!(decide("search YouTube for lofi jazz", &ctx(), &route("tool"), now()), Decision::Reflex(Reflex::Tool { name: "open_url", .. })));
        assert_eq!(decide("find my resume and open it", &ctx(), &route("tool"), now()), Decision::Plan);
        assert_eq!(decide("I want you to open the Finder app and list all the files in it", &ctx(), &route("agent"), now()), Decision::Plan);
        let unclear = SkillMatch { route: "unclear".into(), ask: "x".into(), ..Default::default() };
        assert!(matches!(decide("undo", &ctx(), &unclear, now()), Decision::Reflex(_)));
    }

    #[test]
    fn shaky_speech_never_acts() {
        let shaky = Context { confidence: 0.58, unsure: 0.30, ..ctx() };
        let skill = SkillMatch { matched: true, ..Default::default() };
        assert!(matches!(decide("I am going to get the message, Discord,", &shaky, &skill, now()), Decision::Clarify(_)));
        assert!(matches!(decide("send it to everyone now", &shaky, &route("tool"), now()), Decision::Clarify(_)));
        assert!(matches!(decide("send hi, do,", &ctx(), &route("tool"), now()), Decision::Clarify(_)));
        assert!(matches!(decide("open, porn hub", &ctx(), &route("tool"), now()), Decision::Clarify(_)));
        assert!(!matches!(decide("okay, open Safari", &ctx(), &route("tool"), now()), Decision::Clarify(_)));
        // a question at that confidence is still answered (it can't change anything)
        assert_eq!(decide("what's the capital of Japan", &Context { confidence: 0.58, ..ctx() }, &route("chat"), now()), Decision::Answer);
        // …but barely heard, it's "say that again" — live log: "what is the visual doing?" (0.53) got an invented answer
        assert!(matches!(decide("okay, what is the visual doing?", &Context { confidence: 0.53, ..ctx() }, &route("tool"), now()), Decision::Clarify(_)));
        // reminders never go to chat (it can only claim to set them)
        assert!(!matches!(decide("I need a reminder for my meeting", &ctx(), &route("chat"), now()), Decision::Answer));
    }

    #[test]
    fn live_log_18_nov() {
        // small talk is chat, even with no server route yet (warm-up)
        assert_eq!(decide("what is up?", &ctx(), &route(""), now()), Decision::Answer);
        assert_eq!(decide("hey, how are you doing?", &ctx(), &route("tool"), now()), Decision::Answer);
        // a question put to DEMASCAS mid-sentence is not side talk
        let later = Context { first_after_wake: false, ..ctx() };
        assert!(matches!(decide("before doing the test can you tell me how many PNG files are in my desktop folder.", &later, &route("tool"), now()),
            Decision::Reflex(Reflex::File { action: crate::reflex::FileAction::Count, .. })));
        assert!(!matches!(decide("before we start, can you tell me what the weather is like in Delhi", &later, &route("tool"), now()), Decision::Ignore(_)));
        assert!(matches!(decide("I want you to list me all the PNG files which are in my desktop folder.", &ctx(), &route("tool"), now()),
            Decision::Reflex(Reflex::File { action: crate::reflex::FileAction::List, .. })));
    }

    #[test]
    fn live_log_18_nov_evening() {
        let later = Context { first_after_wake: false, ..ctx() };
        // addressed, not side talk; a capability question is chat
        assert_eq!(decide("Can you process image and answer my queries?", &later, &route("tool"), now()), Decision::Answer);
        // a polite request still acts
        assert!(!matches!(decide("can you open Discord", &later, &route("tool"), now()), Decision::Answer | Decision::Lookup | Decision::Ignore(_)));
        // a general question is chat, not the lookup prompt
        assert_eq!(decide("is it really good at talking?", &ctx(), &route(""), now()), Decision::Answer);
        assert_eq!(decide("what's the price of bitcoin right now", &ctx(), &route("tool"), now()), Decision::Lookup);
        // one stray word is background talk
        assert!(matches!(decide("Happy,", &later, &route("unclear"), now()), Decision::Ignore(_)));
        // a whole request after "open," is not a fragment
        assert!(!matches!(decide("open, presentation file which is currently on this laptop.", &ctx(), &route("tool"), now()), Decision::Clarify(_)));
    }

    #[test]
    fn screen_commands_are_not_chat() {
        for t in ["fill in this quiz for me", "tick the I agree box on this page", "place the order", "submit the quiz"] {
            let d = decide(t, &ctx(), &route("chat"), now());
            assert!(!matches!(d, Decision::Answer | Decision::Ignore(_)), "{t}: {d:?}");
        }
    }

    #[test]
    fn multi_step_requests_plan() {
        assert_eq!(decide("find my project report, summarise it and email it to Aman", &ctx(), &route("agent"), now()), Decision::Plan);
        assert_eq!(decide("research electric cars and make a presentation about them", &ctx(), &route("agent"), now()), Decision::Plan);
    }

    #[test]
    fn questions_never_act() {
        // answered from the running apps (read-only), not by the model
        assert_eq!(decide("is Discord open", &ctx(), &route("tool"), now()), Decision::Reflex(Reflex::IsRunning("discord".into())));
        assert_eq!(decide("what's the price of bitcoin right now", &ctx(), &route("tool"), now()), Decision::Lookup);
        assert_eq!(decide("how do I make a new server in Discord", &ctx(), &route("agent"), now()), Decision::Lookup);
        assert!(matches!(decide("what time is it", &ctx(), &route("tool"), now()), Decision::Reflex(Reflex::Say(_))));
        assert_eq!(decide("can you open Discord and go to friends", &ctx(), &route("agent"), now()), Decision::Plan);
    }

    #[test]
    fn work_goes_to_the_loop() {
        assert!(matches!(decide("create a presentation about black holes", &ctx(), &route("agent"), now()), Decision::Reflex(Reflex::Tool { name: "create_presentation", .. })));
        assert!(matches!(decide("delete notes.txt from my desktop", &ctx(), &route("tool"), now()), Decision::Reflex(Reflex::File { action: crate::reflex::FileAction::Delete, .. })));
        assert!(matches!(decide("send hi to Hatim on Discord", &ctx(), &route("agent"), now()), Decision::Reflex(Reflex::Tool { name: "message_contact", .. })));
        assert!(matches!(decide("open Discord", &ctx(), &route("agent"), now()), Decision::Reflex(_)));
    }
}
