//! Timers, alarms and reminders.  Understood without the model, kept in
//! `~/.demascas/timers.json` (they survive a restart), spoken when due.
//!
//!   "set a timer for ten minutes" · "a 5 minute timer" · "half an hour timer"
//!   "wake me up at 7" · "set an alarm for 7:30 am"
//!   "remind me at 6 pm to call mom" · "remind me in 20 minutes to check the oven"
//!   "cancel my timer" · "how long is left on my timer"

use std::path::PathBuf;
use std::sync::Mutex;

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Kind {
    Timer,
    Alarm,
    Reminder,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Event {
    pub id: u64,
    pub kind: Kind,
    /// Unix seconds.
    pub due: i64,
    /// "10 minute" / "call mom"
    pub label: String,
}

impl Event {
    /// What DEMASCAS says when it's due.
    pub fn announcement(&self) -> String {
        match self.kind {
            Kind::Timer => format!("Your {} timer is done.", self.label),
            Kind::Alarm => format!("It's {} — time to get up.", self.label),
            Kind::Reminder => format!("Reminder: {}.", self.label),
        }
    }
}

/// A request understood from speech.
#[derive(Debug, Clone, PartialEq)]
pub enum Request {
    Add { kind: Kind, due: i64, label: String, confirm: String },
    CancelAll,
    HowLong,
}

/// Local wall-clock time (from the OS; no date library needed).
#[derive(Debug, Clone, Copy, Default)]
pub struct Clock {
    pub unix: i64,
    /// Seconds since local midnight.
    pub of_day: i64,
}

impl Clock {
    pub fn now() -> Self {
        let unix = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0);
        let of_day = std::process::Command::new("date").arg("+%H %M %S").output().ok().and_then(|o| {
            let v: Vec<i64> = String::from_utf8_lossy(&o.stdout).split_whitespace().filter_map(|x| x.parse().ok()).collect();
            (v.len() == 3).then(|| v[0] * 3600 + v[1] * 60 + v[2])
        }).unwrap_or(unix.rem_euclid(86_400));
        Self { unix, of_day }
    }

    /// The next time the clock shows `h:m` (today if still ahead, else tomorrow).
    pub fn next_at(&self, h: i64, m: i64) -> i64 {
        let target = h * 3600 + m * 60;
        let mut delta = target - self.of_day;
        if delta <= 0 {
            delta += 86_400;
        }
        self.unix + delta
    }
}

fn num_word(w: &str) -> Option<f64> {
    const W: &[(&str, f64)] = &[
        ("a", 1.0), ("an", 1.0), ("one", 1.0), ("two", 2.0), ("three", 3.0), ("four", 4.0), ("five", 5.0), ("six", 6.0),
        ("seven", 7.0), ("eight", 8.0), ("nine", 9.0), ("ten", 10.0), ("eleven", 11.0), ("twelve", 12.0),
        ("fifteen", 15.0), ("twenty", 20.0), ("thirty", 30.0), ("forty", 40.0), ("forty-five", 45.0), ("fifty", 50.0),
        ("sixty", 60.0), ("ninety", 90.0), ("couple", 2.0), ("few", 3.0),
    ];
    w.parse::<f64>().ok().or_else(|| W.iter().find(|(k, _)| *k == w).map(|(_, v)| *v))
}

fn unit_secs(w: &str) -> Option<i64> {
    let w = w.trim_end_matches('s');
    match w {
        "second" | "sec" => Some(1),
        "minute" | "min" => Some(60),
        "hour" | "hr" => Some(3600),
        _ => None,
    }
}

/// "ten minutes" / "1 hour 30 minutes" / "half an hour" / "90 seconds" → seconds.
pub fn parse_duration(words: &[&str]) -> Option<i64> {
    let joined = words.join(" ");
    if joined.contains("half an hour") || joined.contains("half hour") {
        return Some(1800);
    }
    if joined.contains("quarter of an hour") || joined.contains("quarter hour") {
        return Some(900);
    }
    let mut total = 0i64;
    let mut found = false;
    let mut i = 0;
    while i < words.len() {
        let w = words[i].trim_end_matches(['-', ',']);
        // "10-minute", "5min"
        let (n, rest) = split_num_unit(w);
        if let (Some(n), Some(u)) = (n, rest.and_then(unit_secs)) {
            total += (n * u as f64) as i64;
            found = true;
        } else if let Some(n) = num_word(w) {
            // "twenty five minutes"
            let mut n = n;
            let mut j = i + 1;
            if j < words.len() {
                if let Some(k) = num_word(words[j]).filter(|k| *k < 10.0 && n >= 20.0) {
                    n += k;
                    j += 1;
                }
            }
            if j < words.len() {
                let unit = words[j].trim_end_matches([',', '.']);
                let unit = unit.strip_suffix("-long").unwrap_or(unit);
                if let Some(u) = unit_secs(unit) {
                    total += (n * u as f64) as i64;
                    found = true;
                    i = j;
                } else if words[j] == "and" && j + 3 < words.len() && words[j + 1] == "a" && words[j + 2] == "half" {
                    if let Some(u) = unit_secs(words[j + 3]) {
                        total += ((n + 0.5) * u as f64) as i64;
                        found = true;
                        i = j + 3;
                    }
                }
            }
        }
        i += 1;
    }
    (found && total > 0).then_some(total)
}

fn split_num_unit(w: &str) -> (Option<f64>, Option<&str>) {
    if let Some((a, b)) = w.split_once('-') {
        return (num_word(a), Some(b));
    }
    let k = w.find(|c: char| !c.is_ascii_digit() && c != '.').unwrap_or(w.len());
    if k > 0 && k < w.len() {
        return (w[..k].parse().ok(), Some(&w[k..]));
    }
    (None, None)
}

/// "7" / "7:30" / "7 30" / "seven" + optional am/pm → (h, m) on a 24 h clock.
pub fn parse_clock(words: &[&str], clock: &Clock) -> Option<(i64, i64)> {
    for (i, w) in words.iter().enumerate() {
        let w = w.trim_end_matches(['.', ',', '?']);
        let (mut h, mut m, mut used) = (None, 0i64, 1usize);
        let digits: String = w.chars().take_while(|c| c.is_ascii_digit()).collect();
        if let Some((a, b)) = w.split_once(':') {
            h = a.parse::<i64>().ok();
            m = b.trim_end_matches(|c: char| !c.is_ascii_digit()).parse().unwrap_or(0);
        } else if (3..=4).contains(&digits.len()) && {
            // "338 pm" / "338pm" / "at 1030" — the recogniser drops the colon
            let tail = w[digits.len()..].to_lowercase();
            let next = words.get(i + 1).map(|x| x.to_lowercase()).unwrap_or_default();
            let prev = if i > 0 { words[i - 1] } else { "" };
            tail.starts_with("am") || tail.starts_with("pm") || next.starts_with("am") || next.starts_with("pm") || next.starts_with("a.m") || next.starts_with("p.m") || prev == "at"
        } {
            let (hs, ms) = digits.split_at(digits.len() - 2);
            let (hv, mv) = (hs.parse::<i64>().unwrap_or(99), ms.parse::<i64>().unwrap_or(99));
            if !(0..24).contains(&hv) || !(0..60).contains(&mv) {
                continue;
            }
            h = Some(hv);
            m = mv;
        } else if let Some(n) = num_word(w).filter(|n| n.fract() == 0.0 && *n >= 1.0 && *n <= 24.0 && w != "a" && w != "an") {
            h = Some(n as i64);
            if let Some(next) = words.get(i + 1) {
                if let Ok(mm) = next.parse::<i64>() {
                    if (0..60).contains(&mm) {
                        m = mm;
                        used = 2;
                    }
                }
            }
        }
        let Some(mut hh) = h else { continue };
        let suffix: String = words.get(i + used).map(|s| s.to_lowercase()).unwrap_or_default();
        let inline = w.to_lowercase();
        let pm = suffix.starts_with("pm") || suffix.starts_with("p.m") || inline.ends_with("pm") || words.iter().any(|x| *x == "evening" || *x == "tonight" || *x == "afternoon");
        let am = suffix.starts_with("am") || suffix.starts_with("a.m") || inline.ends_with("am") || words.contains(&"morning");
        if pm && hh < 12 {
            hh += 12;
        } else if am && hh == 12 {
            hh = 0;
        } else if !pm && !am && hh < 12 {
            // no am/pm: the next time the clock shows it
            let today = hh * 3600 + m * 60;
            if today <= clock.of_day && today + 12 * 3600 > clock.of_day && words.contains(&"tomorrow") == false {
                hh += 12;
            }
        }
        return Some((hh % 24, m));
    }
    None
}

pub fn spoken_clock(h: i64, m: i64) -> String {
    let (hh, ap) = match h {
        0 => (12, "AM"),
        1..=11 => (h, "AM"),
        12 => (12, "PM"),
        _ => (h - 12, "PM"),
    };
    format!("{hh}:{m:02} {ap}")
}

pub fn spoken_duration(secs: i64) -> String {
    let (h, m, s) = (secs / 3600, (secs % 3600) / 60, secs % 60);
    let part = |n: i64, unit: &str| if n == 1 { format!("1 {unit}") } else { format!("{n} {unit}s") };
    let mut v = Vec::new();
    if h > 0 {
        v.push(part(h, "hour"));
    }
    if m > 0 {
        v.push(part(m, "minute"));
    }
    if s > 0 && h == 0 {
        v.push(part(s, "second"));
    }
    if v.is_empty() {
        "0 seconds".into()
    } else {
        v.join(" and ")
    }
}

/// "10 minute" (for "your 10 minute timer")
fn label_for(secs: i64) -> String {
    spoken_duration(secs).replace(" minutes", " minute").replace(" hours", " hour").replace(" seconds", " second")
}

fn simplify(text: &str) -> String {
    let mut out = String::new();
    for c in text.to_lowercase().chars() {
        if c.is_alphanumeric() || c == ':' || c == '-' || c == '.' {
            out.push(c);
        } else if c == '\'' || c == '’' {
            continue;
        } else if !out.ends_with(' ') {
            out.push(' ');
        }
    }
    out.trim().trim_end_matches('.').to_string()
}

/// Understand a timer / alarm / reminder request.
pub fn parse(text: &str, clock: &Clock) -> Option<Request> {
    let s = simplify(text);
    let words: Vec<&str> = s.split_whitespace().collect();
    let has = |p: &str| format!(" {s} ").contains(&format!(" {p} "));

    if (has("cancel") || has("stop") || has("delete") || has("clear")) && (has("timer") || has("timers") || has("alarm") || has("alarms") || has("reminder") || has("reminders")) {
        return Some(Request::CancelAll);
    }
    if (has("how long") || has("how much time") || has("time left") || has("whats left")) && (has("timer") || has("alarm") || has("left")) {
        return Some(Request::HowLong);
    }

    // ── reminders: "remind me (at 6 pm|in 20 minutes) to X" / "remind me to X at 6"
    //    also "give me / set / create a reminder at 3:38 pm that I have a meeting"
    let mut s = s.clone();
    for p in ["give me a reminder", "set me a reminder", "set a reminder", "create a reminder", "add a reminder", "make a reminder",
              "put a reminder", "set up a reminder", "i need a reminder", "i want a reminder", "can you remind me", "please remind me"] {
        if let Some(k) = s.find(p) {
            s = format!("remind me{}", &s[k + p.len()..]);
            break;
        }
    }
    if let Some(pos) = s.find("remind me") {
        let rest = &s[pos + "remind me".len()..];
        let rest = rest.strip_prefix(" for").unwrap_or(rest);
        // the task follows "to", "that", or "about"
        let sep = [" to ", " that ", " about "].iter().filter_map(|m| rest.find(m).map(|k| (k, m.len(), *m))).min_by_key(|x| x.0);
        let (when_part, what, that) = match sep {
            Some((k, n, m)) => (format!("{} {}", &rest[..k], ""), rest[k + n..].to_string(), m == " that "),
            None => (rest.to_string(), String::new(), false),
        };
        let mut what = what;
        // "to call mom at 6 pm" — the time may sit after the task
        let mut when_words: Vec<String> = when_part.split_whitespace().map(str::to_string).collect();
        for marker in [" at ", " in ", " tomorrow"] {
            if let Some(k) = what.find(marker) {
                when_words.extend(what[k..].split_whitespace().map(str::to_string));
                what.truncate(k);
            }
        }
        let ww: Vec<&str> = when_words.iter().map(String::as_str).collect();
        let what = format!(" {} ", what.trim()).replace(" my ", " your ").replace(" i have ", " you have ").replace(" im ", " youre ").replace(" i am ", " you are ").replace(" i ", " you ");
        let what = what.trim().trim_start_matches("about ").to_string();
        let label = if what.is_empty() { "your reminder".to_string() } else { what };
        // "that you have a meeting" reads as a statement, "to call mom" as a task
        let say_what = |when: String| if that { format!("Okay, I'll remind you {when} that {label}.") } else { format!("Okay, I'll remind you to {label} {when}.") };
        if ww.contains(&"in") {
            if let Some(d) = parse_duration(&ww) {
                return Some(Request::Add { kind: Kind::Reminder, due: clock.unix + d, label: label.clone(), confirm: say_what(format!("in {}", spoken_duration(d))) });
            }
        }
        if let Some((h, m)) = parse_clock(&ww, clock) {
            let mut due = clock.next_at(h, m);
            if ww.contains(&"tomorrow") && due - clock.unix < 86_400 && clock.next_at(h, m) - clock.unix < 86_400 && (h * 3600 + m * 60) > clock.of_day {
                due += 86_400;
            }
            return Some(Request::Add { kind: Kind::Reminder, due, label: label.clone(), confirm: say_what(format!("at {}", spoken_clock(h, m))) });
        }
        return None;
    }

    // ── alarms: "wake me up at 7", "set an alarm for 7:30 am"
    if has("wake me") || has("alarm") {
        if let Some((h, m)) = parse_clock(&words, clock) {
            let due = clock.next_at(h, m);
            return Some(Request::Add { kind: Kind::Alarm, due, label: spoken_clock(h, m), confirm: format!("Alarm set for {}.", spoken_clock(h, m)) });
        }
        return None;
    }

    // ── timers: "set a timer for ten minutes", "10 minute timer"
    if has("timer") || (has("countdown")) {
        if let Some(d) = parse_duration(&words) {
            return Some(Request::Add { kind: Kind::Timer, due: clock.unix + d, label: label_for(d), confirm: format!("Timer set for {}.", spoken_duration(d)) });
        }
    }
    None
}

/// The saved schedule.
pub struct Schedule {
    path: Option<PathBuf>,
    events: Mutex<Vec<Event>>,
}

impl Schedule {
    /// `~/.demascas/timers.json`
    pub fn load_default() -> Self {
        let path = std::env::var("HOME").ok().map(|h| PathBuf::from(h).join(".demascas").join("timers.json"));
        Self::load(path)
    }

    pub fn load(path: Option<PathBuf>) -> Self {
        let events = path.as_ref().and_then(|p| std::fs::read_to_string(p).ok()).and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default();
        Self { path, events: Mutex::new(events) }
    }

    pub fn in_memory() -> Self {
        Self { path: None, events: Mutex::new(Vec::new()) }
    }

    fn save(&self, ev: &[Event]) {
        if let Some(p) = &self.path {
            if let Some(dir) = p.parent() {
                let _ = std::fs::create_dir_all(dir);
            }
            let tmp = p.with_extension("json.tmp");
            if std::fs::write(&tmp, serde_json::to_string_pretty(ev).unwrap_or_default()).is_ok() {
                let _ = std::fs::rename(tmp, p);
            }
        }
    }

    pub fn add(&self, kind: Kind, due: i64, label: &str) -> Event {
        let mut ev = self.events.lock().unwrap();
        let id = ev.iter().map(|e| e.id).max().unwrap_or(0) + 1;
        let e = Event { id, kind, due, label: label.to_string() };
        ev.push(e.clone());
        self.save(&ev);
        e
    }

    pub fn clear(&self) -> usize {
        let mut ev = self.events.lock().unwrap();
        let n = ev.len();
        ev.clear();
        self.save(&ev);
        n
    }

    pub fn pending(&self) -> Vec<Event> {
        let mut v = self.events.lock().unwrap().clone();
        v.sort_by_key(|e| e.due);
        v
    }

    /// Events that are due now (removed from the schedule).
    pub fn take_due(&self, now_unix: i64) -> Vec<Event> {
        let mut ev = self.events.lock().unwrap();
        let (due, keep): (Vec<Event>, Vec<Event>) = ev.drain(..).partition(|e| e.due <= now_unix);
        *ev = keep;
        if !due.is_empty() {
            self.save(&ev);
        }
        due
    }

    /// Run a parsed request → what to say.
    pub fn apply(&self, r: &Request, clock: &Clock) -> String {
        match r {
            Request::Add { kind, due, label, confirm } => {
                self.add(*kind, *due, label);
                confirm.clone()
            }
            Request::CancelAll => match self.clear() {
                0 => "You don't have any timers or reminders.".into(),
                1 => "Cancelled it.".into(),
                n => format!("Cancelled all {n}."),
            },
            Request::HowLong => match self.pending().first() {
                None => "You don't have any timers running.".into(),
                Some(e) => format!("{} left on your {}.", spoken_duration((e.due - clock.unix).max(0)), match e.kind {
                    Kind::Timer => format!("{} timer", e.label),
                    Kind::Alarm => "alarm".to_string(),
                    Kind::Reminder => "reminder".to_string(),
                }),
            },
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn at(h: i64, m: i64) -> Clock {
        Clock { unix: 1_000_000, of_day: h * 3600 + m * 60 }
    }

    fn add(r: Option<Request>) -> (Kind, i64, String, String) {
        match r {
            Some(Request::Add { kind, due, label, confirm }) => (kind, due, label, confirm),
            other => panic!("not an add: {other:?}"),
        }
    }

    #[test]
    fn timers() {
        let c = at(15, 0);
        let (k, due, label, say) = add(parse("set a timer for ten minutes", &c));
        assert_eq!((k, due - c.unix, label.as_str(), say.as_str()), (Kind::Timer, 600, "10 minute", "Timer set for 10 minutes."));
        assert_eq!(add(parse("set a 5 minute timer", &c)).1 - c.unix, 300);
        assert_eq!(add(parse("timer for half an hour", &c)).1 - c.unix, 1800);
        assert_eq!(add(parse("start a timer for 1 hour 30 minutes", &c)).1 - c.unix, 5400);
        assert_eq!(add(parse("set a timer for twenty five minutes", &c)).1 - c.unix, 1500);
        assert!(parse("what is a timer", &c).is_none());
    }

    #[test]
    fn alarms_and_reminders() {
        let c = at(15, 0); // 3 PM
        let (k, due, _, say) = add(parse("wake me up at 7 tomorrow", &c));
        assert_eq!(k, Kind::Alarm);
        assert_eq!(say, "Alarm set for 7:00 AM.");
        assert_eq!(due - c.unix, 16 * 3600); // 3 PM → 7 AM
        let (k, due, label, say) = add(parse("remind me at 6 pm to call mom", &c));
        assert_eq!((k, label.as_str()), (Kind::Reminder, "call mom"));
        assert_eq!(due - c.unix, 3 * 3600);
        assert_eq!(say, "Okay, I'll remind you to call mom at 6:00 PM.");
        // live log: "give me a reminder at 338 pm that I have a meeting." went to chat and was never set
        let (k, due, label, say) = add(parse("give me a reminder at 338 pm that I have a meeting.", &c));
        assert_eq!((k, label.as_str()), (Kind::Reminder, "you have a meeting"));
        assert_eq!(due - c.unix, 38 * 60);
        assert_eq!(say, "Okay, I'll remind you at 3:38 PM that you have a meeting.");
        assert_eq!(add(parse("set a reminder for 1030 am to call the bank", &c)).2, "call the bank");
        assert_eq!(add(parse("remind me at 337pm to stretch", &c)).1 - c.unix, 37 * 60);
        assert!(parse("what is 338 plus 2", &c).is_none());
        let (_, due, label, _) = add(parse("remind me in 20 minutes to check the oven", &c));
        assert_eq!((due - c.unix, label.as_str()), (1200, "check the oven"));
        let (_, due, label, _) = add(parse("remind me to call mom at 6", &c));
        assert_eq!((due - c.unix, label.as_str()), (3 * 3600, "call mom")); // "6" after 3 PM = 6 PM
        assert_eq!(add(parse("set an alarm for 7:30 am", &c)).3, "Alarm set for 7:30 AM.");
    }

    #[test]
    fn schedule_due_and_cancel() {
        let s = Schedule::in_memory();
        let c = at(10, 0);
        s.apply(&parse("set a timer for 1 minute", &c).unwrap(), &c);
        assert_eq!(s.apply(&Request::HowLong, &c), "1 minute left on your 1 minute timer.");
        assert!(s.take_due(c.unix + 30).is_empty());
        let due = s.take_due(c.unix + 61);
        assert_eq!(due[0].announcement(), "Your 1 minute timer is done.");
        assert_eq!(s.apply(&Request::CancelAll, &c), "You don't have any timers or reminders.");
        assert_eq!(parse("cancel my timer", &c), Some(Request::CancelAll));
    }
}
