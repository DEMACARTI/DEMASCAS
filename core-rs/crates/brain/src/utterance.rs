//! What kind of thing was said — ported from the helpers in
//! `daemon/src/main.cpp` with the same word lists, so behaviour matches.
//! Everything here is instant and deterministic (no model).

use std::collections::HashSet;

/// Lowercase, drop apostrophes ("that's" → "thats"), turn every other
/// non-alphanumeric run into one space.
pub fn simplify(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for c in text.chars().flat_map(char::to_lowercase) {
        if c.is_alphanumeric() {
            out.push(c);
        } else if c == '\'' || c == '’' {
            continue;
        } else if !out.is_empty() && !out.ends_with(' ') {
            out.push(' ');
        }
    }
    out.truncate(out.trim_end().len());
    out
}

/// Whole-word / whole-phrase test on simplified text ("one" ∉ "someone").
pub fn has_word(simplified: &str, phrase: &str) -> bool {
    format!(" {simplified} ").contains(&format!(" {phrase} "))
}

fn has_any(simplified: &str, phrases: &[&str]) -> bool {
    phrases.iter().any(|p| has_word(simplified, p))
}

/// Remove filler words from simplified text.
pub fn strip_fillers(simplified: &str, fillers: &[&str]) -> String {
    let f: HashSet<&str> = fillers.iter().copied().collect();
    simplified.split_whitespace().filter(|w| !f.contains(w)).collect::<Vec<_>>().join(" ")
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Answer {
    Yes,
    No,
    /// Not a yes or a no — a destructive action needs a clear yes, and a new
    /// request in its place is processed as a new request.
    Unclear,
}

/// A spoken reply to a yes/no question.  Any negation wins ("I'm not sure",
/// "no, please don't", "don't do it" are all No).
pub fn classify_answer(text: &str) -> Answer {
    let s = simplify(text);
    const NEG: &[&str] = &[
        "no", "nope", "nah", "not", "dont", "do not", "cancel", "stop", "never", "wait", "hold on",
        "negative", "abort", "nothing", "skip", "leave it", "forget it", "wrong",
    ];
    const YES: &[&str] = &[
        "yes", "yeah", "yep", "yup", "sure", "go ahead", "do it", "proceed", "confirm", "confirmed",
        "okay", "ok", "alright", "affirmative", "absolutely", "of course", "definitely", "please do",
        "go for it", "send it", "delete it", "sounds good", "correct", "right",
    ];
    if has_any(&s, NEG) {
        Answer::No
    } else if has_any(&s, YES) {
        Answer::Yes
    } else {
        Answer::Unclear
    }
}

const WAKE_SUBSTRINGS: &[&str] = &[
    "damascus", "demascas", "demascus", "damaskus", "de mascas", "the mascus", "damasco", "demasco",
    "dimascas", "dimascus", "damask", "da mascas", "hey damascus", "hey demascas", "maskers",
    "the mask is", "the mask", "mascas", "mascus", "demas", "wake up",
];

/// Did the recogniser hear the wake word?  ("Damascus", "the mask is" … are
/// how speech models hear "DEMASCAS".)
pub fn is_wake_phrase(text: &str) -> bool {
    let t = text.to_lowercase();
    const JUNK: &[&str] = &[
        "thank you", "thanks for watching", "subscribe", "[music]", "(music)", "you", "bye",
        "the end", "silence", "...", "[blank_audio]", "blank audio",
    ];
    if JUNK.contains(&t.as_str()) {
        return false;
    }
    WAKE_SUBSTRINGS.iter().any(|w| t.contains(w))
}

/// Strict version while awake: only the wake word itself (plus "hey"), so
/// "set an alarm to wake up at 7" is still a command.
pub fn is_wake_only(text: &str) -> bool {
    let s = strip_fillers(&simplify(text), &["hey", "hi", "hello", "ok", "okay", "yo", "um", "uh", "so"]);
    const WAKE: &[&str] = &[
        "damascus", "demascas", "demascus", "damaskus", "de mascas", "the mascus", "damasco", "demasco",
        "dimascas", "dimascus", "damask", "da mascas", "maskers", "the mask is", "the mask", "mascas",
        "mascus", "demas", "wake up",
    ];
    if WAKE.contains(&s.as_str()) {
        return true;
    }
    // "Maskers Damascus." / "skies, Damascus." (live 18 Nov: both got a dictionary
    // answer about maskers) — the name, plus at most one stray non-command word
    let mut rest = format!(" {s} ");
    let mut named = false;
    let mut sorted: Vec<&str> = WAKE.to_vec();
    sorted.sort_by_key(|w| std::cmp::Reverse(w.len()));
    for w in sorted {
        let pat = format!(" {w} ");
        while let Some(i) = rest.find(&pat) {
            rest.replace_range(i..i + pat.len(), " ");
            named = true;
        }
    }
    let left: Vec<&str> = rest.split_whitespace().collect();
    named && left.len() <= 1 && !left.iter().any(|w| {
        is_cancel_request(w) || is_dismiss(w) || ["stop", "wait", "yes", "no", "help", "undo", "hello", "hi"].contains(w)
    })
}

/// Speech-recogniser phantoms from silence or noise.
pub fn is_hallucination(text: &str) -> bool {
    let t = text.trim().to_lowercase();
    if t.is_empty() {
        return true;
    }
    const KNOWN: &[&str] = &[
        "[blank_audio]", "blank_audio", "blank audio", "thank you.", "thank you", "thanks for watching.",
        "thanks for watching", "thank you for watching.", "thank you for watching", "like and subscribe",
        "subscribe", "you", "the end.", "the end", "...", "..", ".", "silence", "[silence]", "(silence)",
        "[music]", "(music)", "(upbeat music)", "(soft music)", "[no audio]", "(no audio)",
        "[inaudible]", "(inaudible)",
    ];
    if KNOWN.contains(&t.as_str()) {
        return true;
    }
    let bracketed = t.chars().count() >= 3
        && ((t.starts_with('[') && t.ends_with(']')) || (t.starts_with('(') && t.ends_with(')')));
    bracketed || t.chars().filter(|c| c.is_alphanumeric()).count() <= 2
}

/// The recogniser sometimes loops ("open discord, open, discord, open, …"):
/// a 1–4 word phrase repeated 3+ times in a row is cut to one copy, and a
/// dangling partial copy after it is dropped.
pub fn collapse_repeats(text: &str) -> String {
    let mut words: Vec<String> = text.split_whitespace().map(str::to_string).collect();
    let key = |w: &str| w.chars().filter(|c| c.is_alphanumeric()).flat_map(char::to_lowercase).collect::<String>();
    let mut keys: Vec<String> = words.iter().map(|w| key(w)).collect();
    let mut changed = false;
    for n in 1..=4usize {
        let (mut ow, mut ok) = (Vec::new(), Vec::new());
        let mut i = 0;
        while i < keys.len() {
            let mut reps = 1;
            if i + n <= keys.len() {
                while i + (reps + 1) * n <= keys.len() && keys[i..i + n] == keys[i + reps * n..i + (reps + 1) * n] {
                    reps += 1;
                }
            }
            if reps >= 3 {
                ow.extend_from_slice(&words[i..i + n]);
                ok.extend_from_slice(&keys[i..i + n]);
                let mut next = i + reps * n;
                let mut p = 0;
                while next + p < keys.len() && p < n && keys[next + p] == keys[i + p] {
                    p += 1;
                }
                if next + p == keys.len() {
                    next = keys.len();
                }
                i = next;
                changed = true;
            } else {
                ow.push(words[i].clone());
                ok.push(keys[i].clone());
                i += 1;
            }
        }
        words = ow;
        keys = ok;
    }
    if !changed {
        return text.to_string();
    }
    words.join(" ").trim_end_matches([',', ' ']).to_string()
}

/// "You did nothing" / "it didn't work" — a complaint about the last task.
pub fn is_complaint(text: &str) -> bool {
    has_any(&simplify(text), &[
        "you did nothing", "you didnt do", "you did not do", "nothing happened", "it didnt work",
        "that didnt work", "it did not work", "not what i asked", "you didnt send", "you did not send",
        "you didnt open", "you didnt even", "that is not what i", "thats not what i", "you only",
        "still not done", "you havent done", "you have not done",
    ])
}

/// A chat reply that CLAIMS an action it can't have done ("Sending a hi…").
pub fn claims_action(reply: &str) -> bool {
    has_any(&simplify(reply), &[
        "sending a", "sending your", "sending the", "sending it", "sending that", "ive sent", "i have sent",
        "message sent", "ill send", "i will send", "im sending", "i am sending", "opening it", "ive opened",
        "i have opened", "im opening", "i am opening", "i just sent", "i just opened",
        // live log: "I'll set a reminder for you at 3:38 PM" — nothing was set
        "ill set", "ive set", "i have set", "i will set", "im setting", "i am setting", "reminder set", "alarm set", "timer set",
        "ill remind you", "i will remind you", "ive added", "i have added", "ill add", "i will add", "ive created", "i have created",
        "ill create", "ive scheduled", "ill schedule", "ive saved", "ill save", "ive deleted", "ill delete", "ive booked",
    ])
}

/// "Stop", "goodbye", "that's enough for the day" — the WHOLE utterance
/// (minus fillers) must be a dismissal, so "open the stopwatch" or "is the
/// download done" are still commands.
pub fn is_dismiss(text: &str) -> bool {
    const FILLERS: &[&str] = &[
        "okay", "ok", "alright", "thanks", "thank", "you", "please", "demascas", "damascus", "hey", "now",
        "for", "just", "so", "well", "um", "uh", "yeah", "right", "then",
    ];
    let s = strip_fillers(&simplify(text), FILLERS);
    if s.is_empty() {
        return false;
    }
    const DISMISS: &[&str] = &[
        "stop", "stop it", "stop talking", "shut up", "be quiet", "quiet", "go to sleep", "go sleep",
        "sleep", "goodbye", "good bye", "bye", "bye bye", "thats all", "that is all", "thats it",
        "that will be all", "thatll be all", "never mind", "nevermind", "nah", "can stop", "enough",
        "im done", "i am done", "were done", "we are done", "done", "go away", "stand by", "standing by",
        "no", "nope", "no more", "nothing", "nothing else", "see ya", "see you", "later",
        "talk to you later", "thats enough", "stay shut", "stay quiet", "keep quiet", "be silent",
        "stay silent", "silence", "shush", "hush", "zip it", "quiet please", "standby", "go standby", "exit", "exit exit",
        "go and standby", "go on standby", "go to standby", "stand by now",
    ];
    if DISMISS.contains(&s.as_str()) {
        return true;
    }
    let full = simplify(text);
    // "…bye." at the end — unless it's a message ("say bye to Aman")
    let (first, _) = full.split_once(' ').unwrap_or((&full, ""));
    let relays = ["say", "send", "tell", "text", "message", "reply", "write", "type", "wish"].contains(&first);
    let bye_end = !relays && ["bye", "goodbye", "bye bye", "see you", "see ya", "later"].iter().any(|b| full.ends_with(&format!(" {b}")));
    bye_end || has_any(&full, &[
        "enough for the day", "enough for today", "done for the day", "done for today", "i want to stop",
        "stop for now", "stop for today", "thats all for now", "thats all for today", "good night",
        "goodnight", "talk to you tomorrow",
        // live log 18 Nov: each of these was taken as a file name
        "go and standby", "go and stand by", "go to standby", "go on standby", "standby for now", "stand by for now",
        "dont need you", "do not need you", "dont need anything", "shut down yourself", "shut yourself down",
        "turn yourself off", "switch yourself off", "go offline", "talk to you later", "that will be all",
    ])
}

/// Is the WHOLE utterance feedback on the last result?  +1 / -1 / 0.
pub fn feedback_polarity(text: &str) -> i32 {
    const FILLERS: &[&str] = &[
        "demascas", "damascus", "hey", "oh", "um", "uh", "well", "so", "that", "thats", "was", "is", "it",
        "you", "really", "very", "a", "the", "thank", "thanks", "ok", "okay", "yeah", "wow", "nice", "work",
    ];
    let s = strip_fillers(&simplify(text), FILLERS);
    const POS: &[&str] = &[
        "good job", "great job", "well done", "perfect", "exactly", "correct", "good", "great", "awesome",
        "excellent", "brilliant", "spot on", "exactly what i wanted", "just what i wanted", "worked",
        "it worked", "good one", "nailed it", "right",
    ];
    const NEG: &[&str] = &[
        "wrong", "not right", "not what i asked", "not what i wanted", "not what i meant", "you got wrong",
        "got wrong", "bad", "incorrect", "didnt", "didnt work", "not working", "you messed up", "messed up",
        "thats not it", "not it", "wrong one", "terrible", "useless",
    ];
    if s.is_empty() {
        0
    } else if POS.contains(&s.as_str()) {
        1
    } else if NEG.contains(&s.as_str()) {
        -1
    } else {
        0
    }
}

/// "What are you doing?" — asked while a task runs.
pub fn is_status_question(text: &str) -> bool {
    has_any(&simplify(text), &[
        "what are you doing", "whatre you doing", "what are you working on", "status", "how is it going",
        "hows it going", "are you done", "are you finished", "how long", "still working", "progress",
        "whats taking so long", "what is taking so long", "is it done", "are you still", "where are you at",
    ])
}

/// "Stop" / "cancel" — said while a task runs.
pub fn is_cancel_request(text: &str) -> bool {
    has_any(&simplify(text), &[
        "cancel", "cancel that", "stop that", "stop it", "stop the task", "abort", "never mind",
        "nevermind", "forget it", "dont do that", "do not do that", "stop working", "stop doing that", "stop",
    ])
}

/// Said DEMASCAS's name (as the recogniser hears it).
pub fn names_demascas(text: &str) -> bool {
    let s = simplify(text);
    has_any(&s, &["demascas", "damascus", "demascus", "the mask is"])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn name_plus_noise_is_wake_only() {
        assert!(is_wake_only("Maskers Damascus."));
        assert!(is_wake_only("skies, Damascus."));
        assert!(!is_wake_only("Damascus, stop"));
        assert!(!is_wake_only("Damascus open Safari"));
    }

    #[test]
    fn dismissals_from_live_log() {
        for t in ["all right, go and standby for now.", "I don't need you right now, bye.", ", shut down yourself.", "standby", "okay bye"] {
            assert!(is_dismiss(t), "{t}");
        }
        for t in ["say bye to Hatim on Discord", "send goodbye to mom", "open the standby settings app", "shut down the computer"] {
            assert!(!is_dismiss(t), "{t}");
        }
    }

    #[test]
    fn simplify_and_words() {
        assert_eq!(simplify("That's  enough, for the DAY!"), "thats enough for the day");
        assert!(has_word("open the stopwatch", "open"));
        assert!(!has_word("someone", "one"));
    }

    #[test]
    fn answers() {
        for y in ["yes", "yeah sure", "go ahead", "send it", "ok"] {
            assert_eq!(classify_answer(y), Answer::Yes, "{y}");
        }
        for n in ["no", "I'm not sure", "no, please don't", "don't do it", "wait", "cancel"] {
            assert_eq!(classify_answer(n), Answer::No, "{n}");
        }
        assert_eq!(classify_answer("open the finder app"), Answer::Unclear);
    }

    #[test]
    fn wake_and_junk() {
        assert!(is_wake_phrase("Damascus"));
        assert!(is_wake_phrase("hey the mask is"));
        assert!(!is_wake_phrase("thank you"));
        assert!(is_wake_only("hey Damascus"));
        assert!(!is_wake_only("set an alarm to wake up at 7"));
        for h in ["Thank you.", "[BELL RINGS]", "(clicking)", "ok", "", "  "] {
            assert!(is_hallucination(h), "{h:?}");
        }
        assert!(!is_hallucination("open Discord"));
    }

    #[test]
    fn repeats_are_collapsed() {
        assert_eq!(collapse_repeats("open discord, open discord, open discord, open"), "open discord");
        assert_eq!(collapse_repeats("go go go now"), "go now");
        assert_eq!(collapse_repeats("open the door"), "open the door");
    }

    #[test]
    fn dismiss_is_whole_utterance() {
        for d in ["stop", "okay thanks, goodbye", "stay shut", "that's enough for the day, I want to stop now", "never mind"] {
            assert!(is_dismiss(d), "{d}");
        }
        for c in ["open the stopwatch", "is the download done", "play music to sleep"] {
            assert!(!is_dismiss(c), "{c}");
        }
    }

    #[test]
    fn task_control_and_claims() {
        assert!(is_status_question("what are you doing"));
        assert!(is_cancel_request("cancel that"));
        assert!(is_complaint("you did nothing"));
        assert!(claims_action("Sending a hi to Hatem now"));
        assert!(!claims_action("Paris is the capital of France."));
        assert!(claims_action("Sure thing, Daksh. I'll set a reminder for you at 3:38 PM for your meeting."));
        assert_eq!(feedback_polarity("good job"), 1);
        assert_eq!(feedback_polarity("that's wrong"), -1);
        assert_eq!(feedback_polarity("open the wrong folder"), 0);
        assert!(names_demascas("Damascus, tell me another one"));
    }
}
