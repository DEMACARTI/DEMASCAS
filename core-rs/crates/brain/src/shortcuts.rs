//! Keyboard shortcuts for common requests ("undo", "new tab", "screenshot")
//! — matched without the model in well under a millisecond.  Ported from
//! `daemon/src/shortcut_map.hpp` (same table, same matching rules).

use std::collections::HashSet;

/// Generated from daemon/src/shortcut_map.hpp (phrase → macOS key combo).
pub const TABLE: &[(&str, &str)] = &[
    ("close window", "command+w"),
    ("close tab", "command+w"),
    ("quit app", "command+q"),
    ("close app", "command+q"),
    ("exit app", "command+q"),
    ("quit application", "command+q"),
    ("close application", "command+q"),
    ("minimize", "command+m"),
    ("minimize all", "command+option+m"),
    ("hide app", "command+h"),
    ("hide others", "command+option+h"),
    ("new window", "command+n"),
    ("new tab", "command+t"),
    ("close all windows", "command+option+w"),
    ("close powerpoint", "command+q"),
    ("close powerpoint app", "command+q"),
    ("close power point app", "command+q"),
    ("close powerpoint presentation", "command+q"),
    ("close the powerpoint presentation", "command+q"),
    ("full screen", "command+control+f"),
    ("toggle full screen", "command+control+f"),
    ("switch app", "command+tab"),
    ("switch window", "command+`"),
    ("force quit", "command+option+escape"),
    ("next tab", "control+tab"),
    ("previous tab", "control+shift+tab"),
    ("undo", "command+z"),
    ("redo", "command+shift+z"),
    ("cut", "command+x"),
    ("copy", "command+c"),
    ("paste", "command+v"),
    ("paste plain", "command+shift+v"),
    ("select all", "command+a"),
    ("find", "command+f"),
    ("find replace", "command+option+f"),
    ("find next", "command+g"),
    ("find previous", "command+shift+g"),
    ("bold", "command+b"),
    ("italic", "command+i"),
    ("underline", "command+u"),
    ("save", "command+s"),
    ("save as", "command+shift+s"),
    ("open file", "command+o"),
    ("print", "command+p"),
    ("reopen tab", "command+shift+t"),
    ("spotlight", "command+space"),
    ("search", "command+space"),
    ("screenshot", "command+shift+3"),
    ("screenshot region", "command+shift+4"),
    ("screenshot window", "command+shift+4, space"),
    ("screen recording", "command+shift+5"),
    ("lock screen", "command+control+q"),
    ("log out", "command+shift+q"),
    ("show desktop", "command+f3"),
    ("mission control", "control+up"),
    ("app expose", "control+down"),
    ("left space", "control+left"),
    ("right space", "control+right"),
    ("emoji", "command+control+space"),
    ("zoom in", "command+="),
    ("zoom out", "command+-"),
    ("reset zoom", "command+0"),
    ("refresh", "command+r"),
    ("reload", "command+r"),
    ("preferences", "command+,"),
    ("settings", "command+,"),
    ("trash", "command+delete"),
    ("empty trash", "command+shift+delete"),
    ("back", "command+["),
    ("forward", "command+]"),
    ("address bar", "command+l"),
    ("url bar", "command+l"),
    ("bookmark", "command+d"),
    ("bookmarks", "command+shift+b"),
    ("downloads", "command+option+l"),
    ("private window", "command+shift+n"),
    ("incognito", "command+shift+n"),
];

fn normalize(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.trim().to_lowercase().chars() {
        let c = if ".,!?;:\"\t\n".contains(c) { ' ' } else { c };
        if c == ' ' && (out.is_empty() || out.ends_with(' ')) {
            continue;
        }
        out.push(c);
    }
    out.truncate(out.trim_end().len());
    out
}

fn has_phrase(text: &str, phrase: &str) -> bool {
    format!(" {text} ").contains(&format!(" {phrase} "))
}

const QUESTION_STARTS: &[&str] = &[
    "how", "what", "what's", "whats", "why", "when", "where", "which", "who", "is", "are", "does",
    "should", "could", "would", "will", "explain", "tell", "describe",
];

const FILLER: &[&str] = &[
    "please", "can", "could", "would", "will", "you", "the", "a", "an", "this", "that", "it", "my", "me",
    "now", "current", "just", "quickly", "go", "ahead", "and", "hey", "demascas", "damascus", "take", "do",
    "window", "app", "for", "right", "okay", "ok", "alright", "then", "thanks", "thank", "kindly", "hit",
    "press", "file", "page", "document", "make", "turn", "to", "again",
];

/// The key combo for a request, if the WHOLE request is a shortcut (its
/// words plus filler).  Questions never match — "how do I lock my screen?"
/// must not lock the screen.  App names ("close Safari") are not shortcuts.
pub fn lookup(text: &str) -> Option<&'static str> {
    let raw = text.trim().to_lowercase();
    if raw.is_empty() || raw.ends_with('?') {
        return None;
    }
    let t = normalize(&raw);
    let tokens: Vec<&str> = t.split_whitespace().collect();
    let first = *tokens.first()?;
    if QUESTION_STARTS.contains(&first) {
        return None;
    }
    // Requests about apps / files / the web are not keyboard shortcuts.
    const NOT_SHORTCUTS: &[&str] = &[
        "summarize", "summarise", "read pdf", "list files", "what files", "show files", "files in", "open",
        "launch", "start", "click", "type", "activate", "switch to", "which app", "what app", "running app",
        "downloads folder", "documents folder", "desktop folder", "search the web", "search online",
        "look up", "google", "browse", "remember", "recall", "scroll", "drag", "list windows",
    ];
    for p in NOT_SHORTCUTS {
        if *p == "open" && t.starts_with("reopen") {
            continue;
        }
        if has_phrase(&t, p) {
            return None;
        }
    }
    for qw in ["close ", "quit ", "exit "] {
        if let Some(after) = t.strip_prefix(qw) {
            let w = after.split(' ').next().unwrap_or("");
            if !["window", "tab", "this", "the", "all", "app", "application"].contains(&w) {
                return None; // "close Safari" is an app request
            }
        }
    }
    if ["close it", "quit it", "exit it"].iter().any(|p| has_phrase(&t, p)) {
        return None;
    }
    if let Some((_, k)) = TABLE.iter().find(|(p, _)| *p == t) {
        return Some(k);
    }
    // Phrase match: only the shortcut's words plus filler words.
    let toks: HashSet<&str> = tokens.iter().copied().collect();
    let filler: HashSet<&str> = FILLER.iter().copied().collect();
    let mut best: Option<(&'static str, usize)> = None;
    for (phrase, keys) in TABLE {
        let pw: Vec<&str> = phrase.split_whitespace().collect();
        if !pw.iter().all(|w| toks.contains(w)) {
            continue;
        }
        if !tokens.iter().all(|t| pw.contains(t) || filler.contains(t)) {
            continue;
        }
        if best.map_or(true, |(_, n)| pw.len() > n) {
            best = Some((keys, pw.len()));
        }
    }
    best.map(|(k, _)| k)
}

#[cfg(test)]
mod tests {
    use super::lookup;

    #[test]
    fn shortcuts_match_whole_requests_only() {
        assert_eq!(lookup("undo"), Some("command+z"));
        assert_eq!(lookup("please take a screenshot"), Some("command+shift+3"));
        assert_eq!(lookup("new tab"), Some("command+t"));
        assert_eq!(lookup("reopen tab"), Some("command+shift+t"));
        assert_eq!(lookup("how do I lock my screen?"), None);
        assert_eq!(lookup("check the logs about the outage"), None);
        assert_eq!(lookup("close Safari"), None);
        assert_eq!(lookup("open Discord"), None);
        assert_eq!(lookup("close it"), None);
    }
}
