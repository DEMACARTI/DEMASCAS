//! The conversation state machine (replaces the loop in `daemon/src/main.cpp`).
//!
//!   SLEEPING ──wake──▶ LISTENING ──request──▶ route ─▶ inline (chat, reflex, ask)
//!                         ▲   │                     └▶ background task (tool loop,
//!                         │   └─ confirm / choose a file / held command        agent, skill)
//!                         └─────── follow-up window, then "Standing by."
//!
//! While a task runs DEMASCAS keeps listening: "stop", "what are you
//! doing?", questions are answered, new work is queued.

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use bridge::{Bridge, PendingConfirm, SkillMatch};
use brain::actions::speakable;
use brain::engine::{Engine, Outcome};
use brain::reflex::{chrono_lite::Now, Recent};
use brain::router::{self, Context, Control, Decision};
use brain::utterance::*;
use tokio::sync::Mutex as AsyncMutex;
use tokio::task::JoinHandle;
use tokio_util::sync::CancellationToken;

use crate::io::{emit_event, Ears, Heard, Mode, Mouth};

pub struct Config {
    /// How long to keep listening after DEMASCAS speaks.
    pub followup: Duration,
    /// Spoken on wake (voice mode); None in text mode.
    pub wake_ack: Option<String>,
    /// Exam: finish background work before taking the next line.
    pub wait_for_tasks: bool,
}

struct Task {
    handle: JoinHandle<Outcome>,
    cancel: CancellationToken,
    command: String,
    started: Instant,
    announced: u8,
}

type Said = Arc<Mutex<VecDeque<(Instant, String)>>>;
/// While answering an iMessage, what DEMASCAS "says" is collected here
/// (and sent back as the reply) instead of being spoken.
type Sink = Arc<Mutex<Option<Vec<String>>>>;

enum Got {
    Heard(Heard),
    /// A timer / reminder came due (already announced).
    Due,
    /// A text from the owner: (text, from).
    Remote(String, String),
}

pub struct Session<E: Ears, M: Mouth + 'static> {
    ears: E,
    mouth: Arc<M>,
    bridge: Bridge,
    main: Engine,
    worker: Arc<AsyncMutex<Engine>>,
    cfg: Config,
    mode: Mode,
    first_after_wake: bool,
    recent: Recent,
    said: Said,
    pending: Option<PendingConfirm>,
    unclear_answers: u8,
    choices: Vec<(String, String)>,
    /// When the list was given, and whether DEMASCAS asked "which one?"
    /// (a plain listing stays pickable but doesn't hold anything up).
    choices_at: Instant,
    choices_asked: bool,
    held: Option<(String, Instant)>,
    last_action: Option<String>,
    task: Option<Task>,
    queue: VecDeque<String>,
    last_task_failed: bool,
    blanks: u8,
    /// Last tiny request that keeps the model's weights in memory.
    last_warm: Instant,
    /// Said "memory is tight" this session already.
    mem_warned: bool,
    /// Handling a request from the queue — it's the user's words, even though
    /// DEMASCAS just said "I'll do that next — …" (live 18 Nov: dropped as echo).
    replaying: bool,
    schedule: Arc<events::Schedule>,
    apps_at: Option<Instant>,
    /// DEMASCAS asked something to finish a request: (request, question, when, file action).
    awaiting: Option<(String, String, Instant, Option<brain::reflex::FileAction>)>,
    /// A plan paused for a yes: the rest of it.
    continuation: Option<String>,
    sink: Sink,
    inbound: Option<tokio::sync::mpsc::Receiver<(String, String)>>,
    last_voice: Instant,
    brief_checked: Option<Instant>,
}

/// The request as DEMASCAS says it back: no wake word or polite opener,
/// from the actual ask, in its words ("your", not "my"), ≤ ~50 chars.
/// Live log: "Still working on alright DEMASCAS, I want you to check what are the latest…".
fn short_cmd(c: &str) -> String {
    let mut t = simplify(c);
    loop {
        let before = t.clone();
        for p in ["alright ", "all right ", "okay ", "ok ", "hey ", "so ", "please ", "demascas ", "damascus ", "can you ", "could you ",
                  "would you ", "will you ", "i want you to ", "i need you to ", "id like you to ", "go ahead and ", "just "] {
            if let Some(r) = t.strip_prefix(p) {
                t = r.to_string();
            }
        }
        if t == before {
            break;
        }
    }
    let t = format!(" {t} ").replace(" my ", " your ").replace(" me ", " you ").replace(" i ", " you ").replace(" im ", " youre ");
    let t = t.trim();
    if t.len() <= 50 {
        return t.to_string();
    }
    let cut = bridge::utf8_cut(t, 50);
    let cut = cut.rsplit_once(' ').map(|x| x.0).unwrap_or(cut);
    format!("{cut}…")
}

/// Its own voice coming back through the mic: ≥ 70 % of the heard words
/// (or their stems — "success" ~ "successfully") were said in the last 20 s.
pub fn is_own_echo(heard: &str, said: &VecDeque<(Instant, String)>) -> bool {
    let words: Vec<String> = simplify(heard).split_whitespace().map(str::to_string).collect();
    if words.len() < 2 {
        return false;
    }
    said.iter().filter(|(t, _)| t.elapsed() < Duration::from_secs(20)).any(|(_, s)| {
        let sw: Vec<String> = simplify(s).split_whitespace().map(str::to_string).collect();
        let hit = words.iter().filter(|w| sw.iter().any(|x| {
            let n = w.len().min(x.len());
            *w == x || (n >= 4 && w.as_bytes()[..n] == x.as_bytes()[..n])
        })).count();
        hit * 10 >= words.len() * 7
    })
}

/// After a barge-in: its own voice, judged against EVERYTHING it said in the
/// last 10 s (live log: "Daksh, just help." stitched from two of its own
/// sentences, then answered).  ≥ 60 % of the words.
pub fn is_own_echo_loose(heard: &str, said: &VecDeque<(Instant, String)>) -> bool {
    let words: Vec<String> = simplify(heard).split_whitespace().map(str::to_string).collect();
    if words.len() < 2 {
        return false;
    }
    let recent: Vec<String> = said.iter().filter(|(t, _)| t.elapsed() < Duration::from_secs(10))
        .flat_map(|(_, s)| simplify(s).split_whitespace().map(str::to_string).collect::<Vec<_>>()).collect();
    let hit = words.iter().filter(|w| recent.iter().any(|x| {
        let n = w.len().min(x.len());
        *w == x || (n >= 4 && w.as_bytes()[..n] == x.as_bytes()[..n])
    })).count();
    hit * 10 >= words.len() * 6
}

/// "the second one", "number 2", "the SIH one" → an offered file.
/// While a task runs, only these get through: "stop", "cancel", "abort",
/// "never mind", "stop the task", "DEMASCAS stop" (short — never a sentence
/// that merely contains the word).
pub fn is_task_stop(text: &str) -> bool {
    let s = strip_fillers(&simplify(text), &["demascas", "damascus", "okay", "ok", "please", "hey", "just", "now", "right"]);
    s.split_whitespace().count() <= 4
        && ["stop", "cancel", "abort", "never mind", "nevermind", "forget it", "enough", "stop it", "stop that", "stop the task",
            "cancel that", "cancel the task", "stop working", "quit it"].iter().any(|p| s == *p || s.starts_with(&format!("{p} ")) || s.ends_with(&format!(" {p}")))
}

/// "- DNG. - Stop!" (Whisper writes overlapping voices as dialogue) / "okay, stop" →
/// "stop", when the utterance ENDS in a stop and the rest is a few stray words.
pub fn trailing_stop(text: &str) -> Option<String> {
    let last = text.split(['.', '!', '?', '-', ',', ';']).map(str::trim).filter(|p| !p.is_empty()).last()?;
    let l = simplify(last);
    const STOP: &[&str] = &["stop", "stop it", "stop stop", "stop talking", "please stop", "okay stop", "ok stop", "shut up", "be quiet", "quiet", "enough", "thats enough"];
    let words = simplify(text).split_whitespace().count();
    (STOP.contains(&l.as_str()) && words <= 6).then(|| last.trim_end_matches(['.', '!']).to_string())
}

/// Could this be the answer to "what's the exact file name?" — a name, not a
/// sentence (live log: "I don't need you right now, bye" was searched for).
pub fn looks_like_file_name(text: &str) -> bool {
    let v = answer_value(text);
    let s = simplify(&v);
    let words: Vec<&str> = s.split_whitespace().collect();
    const NOT_NAME: &[&str] = &["i", "im", "you", "me", "we", "need", "want", "dont", "bye", "goodbye", "go", "yourself",
        "standby", "stop", "right", "now", "please", "never", "mind", "forget", "nothing", "shut"];
    !words.is_empty() && words.len() <= 6 && !words.iter().any(|w| NOT_NAME.contains(w)) && !is_dismiss(text) && !is_cancel_request(text)
}

pub fn resolve_choice(spoken: &str, choices: &[(String, String)]) -> Option<(String, String)> {
    let s = simplify(spoken);
    // "any of them" / "a random one" / "you choose"
    if ["random", "any", "any one", "anyone", "whichever", "doesnt matter", "you choose", "you pick", "your choice"].iter().any(|w| has_word(&s, w)) && !choices.is_empty() {
        let n = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.subsec_nanos() as usize).unwrap_or(0);
        return choices.get(n % choices.len()).cloned();
    }
    const ORD: &[(&str, i32)] = &[
        ("number 1", 0), ("number one", 0), ("number 2", 1), ("number two", 1), ("number 3", 2), ("number three", 2),
        ("number 4", 3), ("number four", 3), ("number 5", 4), ("number five", 4), ("first", 0), ("1st", 0),
        ("second", 1), ("2nd", 1), ("third", 2), ("3rd", 2), ("fourth", 3), ("4th", 3), ("fifth", 4), ("5th", 4),
        ("last", -1), ("one", 0), ("two", 1), ("three", 2), ("four", 3), ("five", 4),
    ];
    for (w, i) in ORD {
        if has_word(&s, w) {
            let idx = if *i < 0 { choices.len() as i32 - 1 } else { *i };
            return choices.get(idx as usize).cloned();
        }
    }
    const IGNORE: &[&str] = &["the", "a", "an", "one", "open", "file", "that", "this", "please", "it"];
    let mut best: Option<(usize, &(String, String))> = None;
    for c in choices {
        let name = simplify(&c.0);
        let score: usize = s.split_whitespace().filter(|w| w.len() >= 2 && !IGNORE.contains(w) && name.contains(*w)).map(str::len).sum();
        if score >= 3 && best.is_none_or(|(b, _)| score > b) {
            best = Some((score, c));
        }
    }
    best.map(|(_, c)| c.clone())
}

impl<E: Ears, M: Mouth + 'static> Session<E, M> {
    pub fn new(ears: E, mouth: Arc<M>, bridge: Bridge, mut main: Engine, mut worker: Engine, cfg: Config, schedule: Arc<events::Schedule>) -> Self {
        // one request log, one schedule and one governance gate for both engines
        let gov = Arc::new(brain::governance::Governance::load_default());
        main.gov = gov.clone();
        worker.gov = gov;
        worker.log = main.log.clone();
        main.schedule = Some(schedule.clone());
        worker.schedule = Some(schedule.clone());
        Self {
            ears, mouth, bridge, main, worker: Arc::new(AsyncMutex::new(worker)), cfg, mode: Mode::Sleeping,
            first_after_wake: false, recent: Recent::default(), said: Arc::new(Mutex::new(VecDeque::new())),
            pending: None, unclear_answers: 0, choices: Vec::new(), choices_at: Instant::now(), choices_asked: false, held: None, last_action: None, task: None,
            queue: VecDeque::new(), last_task_failed: false, blanks: 0, last_warm: Instant::now(), mem_warned: false, replaying: false, schedule, apps_at: None, awaiting: None, continuation: None,
            sink: Arc::new(Mutex::new(None)), inbound: None, last_voice: Instant::now(), brief_checked: None,
        }
    }

    fn speak(&self, text: &str) {
        if text.trim().is_empty() {
            return;
        }
        if let Some(buf) = self.sink.lock().unwrap().as_mut() {
            buf.push(text.to_string());
            return;
        }
        self.mouth.say(text);
        let mut s = self.said.lock().unwrap();
        s.push_back((Instant::now(), text.to_string()));
        while s.len() > 12 {
            s.pop_front();
        }
    }

    /// It asked a real question (not "say that again?") — the next sentence
    /// needs no wake word.  Live log: after "Sorry, I didn't catch that…?"
    /// the TV ("Don't worry, you'll be fine…") was answered.
    fn it_asked(&self) -> bool {
        // the last reply, spoken sentence by sentence ("What kind of test? Tell
        // me what you need." — the question isn't the last sentence)
        let said = self.said.lock().unwrap();
        let Some((last_at, _)) = said.back() else { return false };
        said.iter().rev().take_while(|(at, _)| last_at.duration_since(*at) < Duration::from_secs(8)).any(|(_, s)| {
            let s = s.trim();
            // "Sorry, I didn't catch that — say it again?" counts: what comes next is
            // the request said again (live 18 Nov: the repeat was ignored as side talk)
            s.contains('?')
        })
    }

    /// A 1-token request on the cached chat prompt: touches every weight, so
    /// macOS reads them back from swap NOW (while the user speaks) instead of
    /// during the real request (live 18 Nov: 8–20 s first words, 0.26 s when
    /// hot).  Voice mode only; never awaited; at most every 20 s unless woken.
    fn warm_model(&mut self, woken: bool) {
        if self.cfg.wait_for_tasks || (!woken && self.last_warm.elapsed() < Duration::from_secs(20))
            || self.last_warm.elapsed() < Duration::from_secs(3) {
            return;
        }
        let Some(llm) = self.main.llm.clone() else { return };
        self.last_warm = Instant::now();
        tokio::spawn(async move {
            let t = Instant::now();
            let req = llm::Request {
                messages: vec![
                    serde_json::json!({"role": "system", "content": brain::prompts::chat_system()}),
                    serde_json::json!({"role": "user", "content": "hi"}),
                ],
                max_tokens: 1,
                temperature: 0.0,
            };
            let _ = llm.complete(&req, Duration::from_secs(30)).await;
            let took = t.elapsed().as_secs_f32();
            if took > 1.5 {
                println!("[MEM] model was swapped out — paged back in ({took:.1} s, before your request)");
            }
        });
    }

    /// Once per session, when swap is high: say which app to close.
    fn warn_if_memory_tight(&mut self) {
        if self.cfg.wait_for_tasks || self.mem_warned {
            return;
        }
        let swap = llm::swap_used_mb().unwrap_or(0); // ~3 µs
        if swap < crate::mem::TIGHT_SWAP_MB {
            return;
        }
        self.mem_warned = true;
        let top = crate::mem::latest(); // measured in the background
        println!("[MEM] swap {:.1} GB — biggest: {}", swap as f32 / 1024.0, crate::mem::describe(&top));
        if let Some((app, mb)) = top.first().filter(|(_, mb)| *mb >= 300) {
            // "close it" now means that app (live 18 Nov: got "Close what?")
            self.recent.last_app = Some(app.clone());
            self.speak(&format!("Memory is tight right now, so I may be slow. {app} is using {:.1} gigabytes — closing it would make me faster.", *mb as f32 / 1024.0));
        }
    }

    fn busy(&self) -> bool {
        self.task.as_ref().is_some_and(|t| !t.handle.is_finished())
    }

    /// Speak timers / alarms / reminders that are due.
    fn announce_due(&self) {
        let away = self.last_voice.elapsed() > Duration::from_secs(30 * 60);
        for e in self.schedule.take_due(events::Clock::now().unix) {
            println!("[⏰ DUE] {}", e.announcement());
            self.mouth.chime("Glass");
            self.speak(&e.announcement());
            if away && self.inbound.is_some() {
                // nobody's been talking to me for a while — tell the phone too
                let (bridge, text) = (self.bridge.clone(), e.announcement());
                tokio::spawn(async move { bridge.channel_reply("", &text).await; });
            }
        }
    }

    /// Wait for the ears — but wake up when a timer comes due or a text
    /// from the owner arrives (only then; empty polls never interrupt).
    async fn listen(&mut self, mode: Mode, timeout: Option<Duration>) -> Option<Heard> {
        let mut wait = self.schedule.pending().first()
            .map(|e| Duration::from_secs((e.due - events::Clock::now().unix).max(0) as u64))
            .unwrap_or(Duration::from_secs(86_400 * 365));
        // also wake at 8 am (and every 10 min until noon) to text the brief when away
        if self.inbound.is_some() {
            let of_day = events::Clock::now().of_day;
            let until = if of_day < 8 * 3600 { 8 * 3600 - of_day + 30 } else if of_day < 12 * 3600 { 600 } else { 86_400 - of_day + 8 * 3600 + 30 };
            wait = wait.min(Duration::from_secs(until as u64));
        }
        let got = {
            let ears = &mut self.ears;
            let inbound = self.inbound.as_mut();
            tokio::select! {
                h = ears.next(mode, timeout) => Got::Heard(h),
                _ = tokio::time::sleep(wait) => Got::Due,
                Some((text, from)) = async move { match inbound { Some(rx) => rx.recv().await, None => std::future::pending().await } } => Got::Remote(text, from),
            }
        };
        match got {
            Got::Heard(h) => {
                if matches!(h, Heard::Speech { .. } | Heard::Wake) {
                    self.last_voice = Instant::now();
                }
                Some(h)
            }
            Got::Due => {
                self.announce_due();
                None
            }
            Got::Remote(text, from) => {
                self.handle_remote(text, from).await;
                None
            }
        }
    }

    /// Start polling the owner's texts (iMessage) in the background.
    pub fn enable_channel(&mut self) {
        let (tx, rx) = tokio::sync::mpsc::channel(16);
        let app_tx = tx.clone();
        let bridge = self.bridge.clone();
        tokio::spawn(async move {
            loop {
                let (msgs, on) = bridge.channel_inbox().await;
                for m in msgs {
                    if tx.send(m).await.is_err() {
                        return;
                    }
                }
                tokio::time::sleep(Duration::from_secs(if on { 2 } else { 30 })).await;
            }
        });
        // the DEMASCAS app's YES / NO / STOP buttons (token-protected, localhost)
        crate::control::serve(app_tx);
        self.inbound = Some(rx);
    }

    /// A text from the owner: same brain, same rules — the reply goes back by text.
    async fn handle_remote(&mut self, text: String, from: String) {
        if from == "app" {
            // a button in the DEMASCAS app — exactly as if it was said aloud
            println!("[🖱 APP] {text}");
            self.first_after_wake = true;
            self.handle(text, 1.0, 0.0).await;
            return;
        }
        println!("[📱 IMESSAGE] {from}: {text}");
        *self.sink.lock().unwrap() = Some(Vec::new());
        let mode = self.mode;
        self.first_after_wake = true; // a text is always addressed to DEMASCAS
        self.handle(text, 1.0, 0.0).await;
        let t = Instant::now();
        while self.busy() && t.elapsed() < Duration::from_secs(240) {
            tokio::time::sleep(Duration::from_millis(200)).await;
        }
        self.collect_finished_task().await;
        let lines = self.sink.lock().unwrap().take().unwrap_or_default();
        let mut reply = lines.join(" ");
        if self.pending.is_some() && !reply.contains('?') {
            reply.push_str(" Reply yes or no.");
        }
        if reply.trim().is_empty() {
            reply = "Done.".into();
        }
        println!("[📱 REPLY] {reply}");
        self.bridge.channel_reply(&from, &reply).await;
        if mode == Mode::Sleeping && self.mode == Mode::Listening {
            self.mode = Mode::Sleeping; // a text doesn't wake the room
        }
    }

    /// The morning brief, spoken (or texted when the owner is away).
    async fn run_brief(&mut self) {
        println!("[☀ BRIEF]");
        let m = SkillMatch::default();
        let r = brain::reflex::Reflex::Tool { name: "morning_brief", args: serde_json::json!({}), say: String::new() };
        let cancel = CancellationToken::new();
        let (mouth, said, sink) = (self.mouth.clone(), self.said.clone(), self.sink.clone());
        let mut say = move |s: &str| speak_to(&mouth, &said, &sink, s);
        let out = self.main.run("brief me", Decision::Reflex(r), &m, &cancel, &mut say).await;
        self.absorb("brief me", &out, 1.0);
    }

    /// 8–11 am, nobody's talked to me for 30 min, no brief yet → text it.
    async fn brief_by_text_if_away(&mut self) {
        let c = events::Clock::now();
        if self.inbound.is_none() || !(8 * 3600..12 * 3600).contains(&c.of_day) || self.last_voice.elapsed() < Duration::from_secs(30 * 60) {
            return;
        }
        if self.brief_checked.is_some_and(|t| t.elapsed() < Duration::from_secs(600)) {
            return;
        }
        self.brief_checked = Some(Instant::now());
        if self.bridge.briefed_today().await {
            return;
        }
        *self.sink.lock().unwrap() = Some(Vec::new());
        self.run_brief().await;
        let lines = self.sink.lock().unwrap().take().unwrap_or_default();
        if !lines.is_empty() {
            self.bridge.channel_reply("", &format!("Good morning. {}", lines.join(" "))).await;
        }
    }

    async fn refresh_apps(&mut self) {
        if self.apps_at.is_some_and(|t| t.elapsed() < Duration::from_secs(60)) {
            return;
        }
        let a = self.bridge.apps(false).await;
        if !a.installed.is_empty() || !a.running.is_empty() {
            let mut all = a.installed;
            for r in a.running {
                if !all.contains(&r) {
                    all.push(r);
                }
            }
            self.recent.apps = Arc::new(all);
            self.main.apps = self.recent.apps.clone();
        }
        self.apps_at = Some(Instant::now());
    }

    pub async fn run(mut self) {
        loop {
            self.collect_finished_task().await;
            self.announce_due();
            self.brief_by_text_if_away().await;
            match self.mode {
                Mode::Sleeping => match self.listen(Mode::Sleeping, None).await.unwrap_or(Heard::Silence) {
                    Heard::Wake => {
                        println!("[🔊 WAKE WORD]");
                        // page the model back in while the user is still talking
                        self.warm_model(true);
                        self.mouth.chime("Glass");
                        if let Some(ack) = self.cfg.wake_ack.clone() {
                            self.speak(&ack);
                            self.mouth.wait_done().await;
                        }
                        self.first_after_wake = true;
                        self.blanks = 0;
                        self.warn_if_memory_tight();
                        emit_if_text(&self.cfg, "WAKE");
                        self.mode = Mode::Listening;
                        // the first wake of the morning comes with the day's brief
                        if !self.cfg.wait_for_tasks && events::Clock::now().of_day < 12 * 3600 && !self.bridge.briefed_today().await {
                            self.run_brief().await;
                        }
                    }
                    Heard::Reset => self.reset().await,
                    Heard::Closed => break,
                    _ => {}
                },
                Mode::Listening => {
                    if self.cfg.wait_for_tasks && self.busy() {
                        self.announce_progress();
                        // a line typed while the task runs ("what are you doing?", "stop")
                        if let Some(Heard::Speech { text, confidence, unsure }) = self.ears.try_now() {
                            self.handle(text, confidence, unsure).await;
                            continue;
                        }
                        tokio::time::sleep(Duration::from_millis(50)).await;
                        continue;
                    }
                    if self.task.is_some() && !self.busy() {
                        continue; // finished — collect it first
                    }
                    if !self.busy() {
                        if let Some(next) = self.next_queued() {
                            self.replaying = true;
                            self.handle(next, 1.0, 0.0).await;
                            self.replaying = false;
                            continue;
                        }
                    }
                    self.announce_progress();
                    if !self.busy() {
                        self.warm_model(false); // awake and idle: keep the weights in memory
                    }
                    let timeout = if self.busy() { Duration::from_secs(1) } else { self.cfg.followup };
                    let Some(heard) = self.listen(Mode::Listening, Some(timeout)).await else { continue };
                    match heard {
                        // While a task runs DEMASCAS stays on it (the user's choice, 18 Nov):
                        // only "stop" / "cancel" gets through — a kill switch, never chatter
                        // (live log: "start kalu", "Who also?", "Happy," all interrupted work)
                        Heard::Speech { text, .. } if self.busy() && !self.cfg.wait_for_tasks && !is_task_stop(&text) => {
                            println!("[*] Busy with a task — not listening: {text:?}");
                        }
                        Heard::Speech { text, confidence, unsure } => self.handle(text, confidence, unsure).await,
                        Heard::Silence => {
                            if self.busy() || !self.queue.is_empty() || self.pending.is_some() && self.cfg.wait_for_tasks {
                                continue;
                            }
                            if self.pending.take().is_some() {
                                // no answer to a yes/no question = no
                                println!("[CONFIRM] no answer — cancelled");
                            }
                            println!("[*] No speech — going back to sleep.");
                            self.sleep("Standing by.").await;
                        }
                        Heard::Reset => self.reset().await,
                        Heard::Closed => break,
                        Heard::Wake => {}
                    }
                }
            }
        }
        if let Some(t) = self.task.take() {
            t.cancel.cancel();
            let _ = tokio::time::timeout(Duration::from_secs(5), t.handle).await;
        }
    }

    fn next_queued(&mut self) -> Option<String> {
        if self.pending.is_some() || (self.choices_asked && !self.choices.is_empty()) {
            return None; // finish the open question first (live test: the next part was taken as "no")
        }
        if self.last_task_failed && !self.queue.is_empty() {
            let skipped = self.queue.pop_front().unwrap();
            self.queue.clear();
            self.last_task_failed = false;
            self.speak(&format!("Since that didn't work, I've held off on {}. Say it again if you still want it.", short_cmd(&skipped)));
            self.held = Some((skipped, Instant::now()));
            return None;
        }
        let n = self.queue.pop_front();
        if let Some(c) = &n {
            println!("[▶ NEXT] {c}");
        }
        n
    }

    fn announce_progress(&mut self) {
        let Some(t) = self.task.as_mut() else { return };
        if t.handle.is_finished() {
            return;
        }
        let secs = t.started.elapsed().as_secs();
        let line = match (secs, t.announced) {
            (15.., 0) => Some(format!("Still working on {}.", short_cmd(&t.command))),
            (45.., 1) => Some("This is taking a while. Say stop if you'd like me to give up.".to_string()),
            _ => None,
        };
        if let Some(l) = line {
            t.announced += 1;
            self.speak(&l);
        }
    }

    async fn collect_finished_task(&mut self) {
        if !self.task.as_ref().is_some_and(|t| t.handle.is_finished()) {
            return;
        }
        let t = self.task.take().unwrap();
        let cancelled = t.cancel.is_cancelled();
        let out = t.handle.await.unwrap_or_default();
        println!("[✅ TASK DONE] {}{}", t.command, if cancelled { " (cancelled)" } else { "" });
        if cancelled {
            return;
        }
        self.last_task_failed = !out.success;
        self.absorb(&t.command, &out, 1.0);
    }

    /// Remember what an outcome established for the next turns.
    fn absorb(&mut self, command: &str, out: &Outcome, confidence: f32) {
        if out.last_app.is_some() {
            self.recent.last_app = out.last_app.clone();
        }
        if out.last_tool.is_some() {
            self.recent.last_tool = out.last_tool.clone();
        }
        if out.last_file.is_some() {
            self.recent.last_file = out.last_file.clone();
        }
        self.awaiting = out.asked.clone().filter(|_| out.confirm.is_none() && out.file_choices.len() < 2).map(|q| {
            let file = q.contains("exact file name").then_some(brain::reflex::FileAction::Find);
            (command.to_string(), q, Instant::now(), file)
        });
        if matches!(out.path, "tool" | "agent" | "skill" | "reflex") && !out.tools.is_empty() {
            self.last_action = Some(command.to_string());
        }
        self.continuation = out.continuation.clone();
        if let Some(pc) = &out.confirm {
            println!("[CONFIRM] waiting for yes/no: {}", pc.speech);
            self.pending = Some(pc.clone());
            self.unclear_answers = 0;
        }
        if out.file_choices.len() > 1 {
            self.choices = out.file_choices.clone();
            self.choices_at = Instant::now();
            self.choices_asked = out.reply.trim_end().ends_with('?');
        }
        if out.path == "chat" && confidence < 0.7 {
            self.main.drop_last_exchange();
        }
    }

    async fn sleep(&mut self, line: &str) {
        if let Some(t) = self.task.take() {
            t.cancel.cancel();
            let _ = self.bridge.agent_cancel().await;
        }
        self.queue.clear();
        self.mouth.interrupt();
        self.speak(line);
        self.mouth.wait_done().await;
        self.main.forget_conversation();
        if let Ok(mut w) = self.worker.try_lock() {
            w.forget_conversation();
        }
        self.pending = None;
        // the file list stays pickable for a while ("…open the second one")
        self.choices_asked = false;
        self.awaiting = None;
        self.first_after_wake = false;
        self.mode = Mode::Sleeping;
    }

    async fn reset(&mut self) {
        if let Some(t) = self.task.take() {
            t.cancel.cancel();
            self.bridge.agent_cancel().await;
            let _ = tokio::time::timeout(Duration::from_secs(20), t.handle).await;
        }
        self.queue.clear();
        self.main.forget_conversation();
        self.worker.lock().await.forget_conversation();
        self.pending = None;
        self.choices.clear();
        self.held = None;
        self.last_action = None;
        // forget the conversation, not the computer: the app list stays
        self.recent = Recent { apps: self.recent.apps.clone(), ..Default::default() };
        self.said.lock().unwrap().clear(); // a reset stands for time passing
        self.main.clear_log();
        self.awaiting = None;
        self.continuation = None;
        self.first_after_wake = false;
        self.last_task_failed = false;
        self.blanks = 0;
        self.mode = Mode::Sleeping;
        emit_if_text(&self.cfg, "RESET");
    }

    /// One heard request.
    async fn handle(&mut self, raw: String, confidence: f32, unsure: f32) {
        let mut text = brain::normalize::normalize(&collapse_repeats(raw.trim()));
        // "what time is it, and what's the weather in Delhi" = two requests;
        // a later part inherits the app the first one named
        let parts = brain::reflex::split_compound(&text);
        let now = Now::local();
        // later parts that refer back ("…and email Aman a summary", "…and open it")
        // need the earlier results — that's a plan, not separate requests
        let refers_back = parts.iter().skip(1).any(|p| {
            let s = format!(" {} ", simplify(p));
            [" it ", " them ", " that ", " this ", " summary ", " the result ", " those "].iter().any(|w| s.contains(w))
        });
        let all_quick = !refers_back && parts.iter().all(|p| brain::reflex::reflex(p, &self.recent, now).is_some()
            || brain::reflex::read_messages_request(p).is_some() || brain::router::is_question(p));
        if parts.len() > 1 && all_quick && self.pending.is_none() && !(self.choices_asked && !self.choices.is_empty()) {
            let opens = ["open", "switch", "launch", "go", "bring"].iter().any(|v| simplify(&parts[0]).starts_with(v));
            let first_app = if opens { self.app_named(&parts[0]) } else { None };
            text = parts[0].clone();
            for p in parts.into_iter().skip(1).rev() {
                let ps = simplify(&p);
                let app_action = ["check", "read", "send", "message", "type", "click", "reply", "scroll", "go to", "show"].iter().any(|v| ps.starts_with(v))
                    && !["file", "cv", "resume", "report", "note", "timer", "reminder", "calendar", "email", "mail"].iter().any(|w| ps.contains(w));
                let p = match (&first_app, self.app_named(&p)) {
                    (Some(app), None) if app_action => format!("{p} on {app}"),
                    _ => p,
                };
                self.queue.push_front(p);
            }
            println!("[✂ SPLIT] {text} + {} more", self.queue.len());
        }
        for _ in 0..3 {
            match self.handle_once(&text, confidence, unsure).await {
                Some(again) => text = again, // a non-answer to a question = a new request
                None => return,
            }
        }
    }

    fn app_named(&self, text: &str) -> Option<String> {
        let lower = format!(" {} ", simplify(text));
        let mut apps: Vec<&String> = self.recent.apps.iter().collect();
        apps.sort_by_key(|a| std::cmp::Reverse(a.len()));
        apps.into_iter().find(|a| a.len() >= 3 && lower.contains(&format!(" {} ", simplify(a)))).cloned()
    }

    async fn handle_once(&mut self, text: &str, confidence: f32, unsure: f32) -> Option<String> {
        if is_hallucination(text) {
            println!("[*] Recogniser junk ignored: {text:?}");
            self.blanks += 1;
            if self.blanks >= 2 && !self.busy() && !self.cfg.wait_for_tasks {
                self.sleep("Standing by.").await;
            }
            return None;
        }
        if !self.replaying && (is_own_echo(text, &self.said.lock().unwrap()) || (self.ears.barged() && is_own_echo_loose(text, &self.said.lock().unwrap()))) {
            println!("[*] Ignored own echo: {text:?}");
            return None;
        }
        self.blanks = 0;
        println!("[🗣️  YOU SAID]: \"{text}\" (confidence {confidence:.2})");

        // ── "Stop!" said over DEMASCAS: stop talking, keep listening, keep
        //    what's open (live log: "- DNG. - Stop!" went to the model, 20 s)
        let barged_stop = self.ears.barged() && {
            let s = simplify(text);
            s.split_whitespace().count() <= 5 && (s.ends_with(" stop") || s == "stop")
        };
        if barged_stop && !self.busy() {
            println!("[⏹ QUIET] stopped talking");
            self.mouth.interrupt();
            return None;
        }
        if let Some(stop) = trailing_stop(text) {
            if self.ears.barged() && !self.busy() {
                println!("[⏹ QUIET] stopped talking");
                self.mouth.interrupt();
                return None;
            }
            if stop != text.trim() {
                return Some(stop); // just the "stop" — the router knows what that means
            }
        }

        // ── a yes/no question is open
        if let Some(pc) = self.pending.clone() {
            match classify_answer(text) {
                Answer::Yes => {
                    self.pending = None;
                    let r = self.bridge.confirm(&pc.token, true).await;
                    let s = speakable(&r.result);
                    self.speak(if r.success { if s.is_empty() { "Done." } else { &s } } else if s.is_empty() { "That didn't work." } else { &s });
                    // a plan that paused for this yes carries on
                    if let Some(rest) = self.continuation.take().filter(|_| r.success) {
                        println!("[📋 CONTINUE] {rest}");
                        match rest.strip_prefix(brain::engine::RESUME) {
                            // the same request again, now that this site is OK'd
                            // …told what's already done, so it isn't done twice
                            Some(again) => self.start_task(
                                format!("{again}\n(Already done after the user's OK: {}. Do only what is still left; if nothing is left, just say it's done.)", if s.is_empty() { "that step" } else { &s }),
                                Decision::Act, SkillMatch::default()),
                            None => self.start_task(rest, Decision::Plan, SkillMatch::default()),
                        }
                    }
                    return None;
                }
                Answer::No => {
                    self.pending = None;
                    self.continuation = None;
                    let r = self.bridge.confirm(&pc.token, false).await;
                    let s = speakable(&r.result);
                    self.speak(if s.is_empty() || s == "Cancelled." { "Okay, cancelled." } else { &s });
                    return None;
                }
                Answer::Unclear => {
                    if text.split_whitespace().count() >= 2 {
                        // something else entirely → cancel and treat it as new
                        // (it's said TO DEMASCAS — never side talk)
                        self.pending = None;
                        let _ = self.bridge.confirm(&pc.token, false).await;
                        self.speak("Okay, I won't do that.");
                        self.first_after_wake = true;
                        return Some(text.to_string());
                    }
                    self.unclear_answers += 1;
                    if self.unclear_answers >= 2 {
                        self.pending = None;
                        let _ = self.bridge.confirm(&pc.token, false).await;
                        self.speak("I'll leave it, then.");
                    } else {
                        self.speak("Sorry — yes or no?");
                    }
                    return None;
                }
            }
        }

        // ── several files were offered
        if !self.choices.is_empty() && self.choices_at.elapsed() > Duration::from_secs(120) {
            self.choices.clear();
            self.choices_asked = false;
        }
        if !self.choices.is_empty() {
            // a list nobody asked to pick from is only picked from on purpose
            // ("set a timer for two minutes" must not open file two)
            let s = simplify(text);
            let meant = self.choices_asked || ["open", "show", "pick", "choose", "play", "view"].iter().any(|w| has_word(&s, w));
            if let Some((name, path)) = resolve_choice(text, &self.choices).filter(|_| meant) {
                self.choices.clear();
                self.choices_asked = false;
                let r = self.bridge.execute("open_file", &serde_json::json!({"file_path": path}), false).await;
                let ok = if r.has_success { r.success } else { !brain::actions::is_tool_error(&r.result) };
                self.speak(&if ok { format!("Opening {name}.") } else { format!("I couldn't open {name}.") });
                return None;
            }
            let words = text.split_whitespace().filter(|w| w.chars().any(char::is_alphanumeric)).count();
            if !self.choices_asked {
                // just a list — keep it, carry on with the new request
            } else if words >= 3 {
                self.choices_asked = false; // moved on to something else (the list stays pickable)
            } else {
                self.speak("Which one? Say first, second, or part of the name.");
                return None;
            }
        }

        if is_wake_only(text) {
            self.speak("I'm here. What can I do for you?");
            return None;
        }

        // ── an answer to the question DEMASCAS just asked continues that request
        if let Some((original, question, at, file)) = self.awaiting.take() {
            let fresh = brain::router::is_imperative(text) && text.split_whitespace().count() >= 3;
            // an answer is short and not itself a question
            let answer_like = text.split_whitespace().count() <= 10 && !text.trim_end().ends_with('?')
                && (file.is_none() || looks_like_file_name(text));
            if at.elapsed() < Duration::from_secs(45) && answer_like && !fresh && !is_dismiss(text) && !is_cancel_request(text) {
                println!("[↪ ANSWER] to \"{question}\": {text}");
                let m = SkillMatch::default();
                // "Who should I call?" / "Which one: …?" after a call → call that name
                let call_q = question.starts_with("Who should I call") || (question.starts_with("Which one:") && simplify(&original).contains("call"))
                    || question.contains("Who should I call?");
                let decision = match file {
                    Some(action) => {
                        let name = answer_value(text);
                        Decision::Reflex(brain::reflex::Reflex::File { action, name, folder: None })
                    }
                    None if call_q => {
                        let via = if simplify(&original).contains("facetime") { "facetime_audio" } else { "phone" };
                        Decision::Reflex(brain::reflex::Reflex::Tool {
                            name: "call_contact",
                            args: serde_json::json!({"name": answer_value(text), "via": via}),
                            say: String::new(),
                        })
                    }
                    None => Decision::Act,
                };
                let combined = match file {
                    Some(_) => original.clone(),
                    None if call_q => original.clone(),
                    None => format!("{original}\n(I asked: \"{question}\" — the answer: \"{text}\")"),
                };
                if matches!(decision, Decision::Act) {
                    self.start_task(combined, decision, m);
                } else {
                    let cancel = CancellationToken::new();
                    let (mouth, said) = (self.mouth.clone(), self.said.clone());
                    let sink = self.sink.clone();
                let mut say = move |s: &str| speak_to(&mouth, &said, &sink, s);
                    let out = self.main.run(&combined, decision, &m, &cancel, &mut say).await;
                    self.absorb(&combined, &out, confidence);
                }
                return None;
            }
        }

        // ── "you did nothing" → try the last action again
        if is_complaint(text) && !self.busy() {
            if let Some(last) = self.last_action.clone() {
                self.speak("Sorry about that — let me try again.");
                self.start_task(last, Decision::Act, SkillMatch::default());
                return None;
            }
        }
        // ── "yes, I still want it" → the command held back after a failure
        if let Some((held, at)) = self.held.take() {
            let s = simplify(text);
            if at.elapsed() < Duration::from_secs(60)
                && ["still want", "still wanted", "yes", "do it", "go ahead", "please do"].iter().any(|p| has_word(&s, p))
            {
                println!("[↩ HELD] running: {held}");
                return Some(held);
            }
        }

        // ── route (the server's verdict costs ~3 ms; no model call)
        self.refresh_apps().await;
        self.recent.clock = events::Clock::now();
        let m = self.bridge.match_skill(text).await;
        let command = if m.command.is_empty() { text.to_string() } else { m.command.clone() };
        if !m.note.is_empty() {
            self.speak(&m.note);
        }
        let ctx = Context {
            first_after_wake: self.first_after_wake, it_asked: self.it_asked(), task_busy: self.busy(),
            confidence, unsure, recent: self.recent.clone(),
        };
        let decision = router::decide(&command, &ctx, &m, Now::local());
        println!("[ROUTE] {decision:?} (server route: {:?})", m.route);
        if !matches!(decision, Decision::Ignore(_)) {
            self.first_after_wake = false;
        }

        match decision {
            Decision::Ignore(why) => println!("[*] Ignored — {why}"),
            Decision::Control(Control::Cancel) => {
                if let Some(t) = self.task.take() {
                    println!("[⏹ CANCEL] {}", t.command);
                    t.cancel.cancel();
                    self.bridge.agent_cancel().await;
                }
                self.queue.clear();
                self.mouth.interrupt();
                self.speak("Okay, I've stopped.");
            }
            Decision::Control(Control::Status) => {
                let line = match &self.task {
                    Some(t) if !t.handle.is_finished() => {
                        let st = self.bridge.agent_status().await;
                        let step = st.get("last_tool").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(|s| format!(" — last step: {}", s.replace('_', " "))).unwrap_or_default();
                        format!("I'm working on {}, {} seconds so far{step}.", short_cmd(&t.command), t.started.elapsed().as_secs())
                    }
                    _ => "Nothing right now — I'm free.".to_string(),
                };
                self.speak(&line);
            }
            Decision::Control(Control::Sleep) => {
                let line = if self.busy() { "Okay, stopping. Standing by." } else { "Standing by." };
                self.sleep(line).await;
            }
            Decision::Control(Control::Feedback(good)) => {
                self.bridge.feedback(good).await;
                self.speak(if good { "Thanks, I'll remember that worked." } else { "Sorry about that. I'll avoid doing it that way again." });
            }
            // slow recipes (presentations, research, reading mail/chats, the brief)
            // run in the background so DEMASCAS keeps listening — "stop" works
            Decision::Reflex(r) if is_slow(&r) => {
                if self.busy() {
                    self.queue.push_back(command.clone());
                    self.speak(&format!("I'll do that next — {}.", short_cmd(&command)));
                } else {
                    self.start_task(command, Decision::Reflex(r), m);
                }
            }
            d @ (Decision::Clarify(_) | Decision::Reflex(_) | Decision::Answer) => {
                let cancel = CancellationToken::new();
                let mouth = self.mouth.clone();
                let said = self.said.clone();
                let sink = self.sink.clone();
                let mut say = move |s: &str| speak_to(&mouth, &said, &sink, s);
                let out = self.main.run(&command, d, &m, &cancel, &mut say).await;
                self.absorb(&command, &out, confidence);
            }
            d @ (Decision::Skill | Decision::Lookup | Decision::Act | Decision::Agent | Decision::Plan) => {
                if self.busy() {
                    self.queue.push_back(command.clone());
                    self.speak(&format!("I'll do that next — {}.", short_cmd(&command)));
                } else {
                    self.start_task(command, d, m);
                }
            }
        }
        None
    }

    fn start_task(&mut self, command: String, decision: Decision, m: SkillMatch) {
        println!("[🔧 TASK] {decision:?} — {command}");
        let cancel = CancellationToken::new();
        let worker = self.worker.clone();
        let mouth = self.mouth.clone();
        let said = self.said.clone();
        let sink = self.sink.clone();
        let c = cancel.clone();
        let cmd = command.clone();
        let apps = self.recent.apps.clone();
        let handle = tokio::spawn(async move {
            let mut eng = worker.lock().await;
            eng.apps = apps;
            let c2 = c.clone();
            let mut say = move |s: &str| {
                if !c2.is_cancelled() {
                    speak_to(&mouth, &said, &sink, s);
                }
            };
            eng.run(&cmd, decision, &m, &c, &mut say).await
        });
        self.task = Some(Task { handle, cancel, command, started: Instant::now(), announced: 0 });
    }
}

/// Recipes that take seconds — run as background work, not inline.
fn is_slow(r: &brain::reflex::Reflex) -> bool {
    use brain::reflex::{FileAction, Reflex};
    match r {
        Reflex::Tool { name, .. } => [
            "create_presentation", "create_document", "create_spreadsheet", "research_web", "learn_app", "message_contact",
            "read_chat_messages", "read_mail", "morning_brief", "summarize_latest_pdf", "read_file_text", "read_screen_text", "find_image_by_content", "form_read", "describe_screen", "look_at_image",
        ].contains(name),
        Reflex::Steps { .. } => true,
        Reflex::File { action, .. } => *action == FileAction::Read,
        _ => false,
    }
}

/// "the file name is Daksh_Rathore_CV" / "it's called budget" → the value.
pub fn answer_value(text: &str) -> String {
    let t = text.trim().trim_end_matches(['.', '!', ',']);
    let lower = t.to_lowercase();
    for p in ["the file name is", "the file is called", "the name is", "its called", "it's called", "it is called", "file name is", "it's", "its", "it is", "called", "named", "the file is"] {
        if let Some(i) = lower.find(p) {
            if i < 4 {
                return t[i + p.len()..].trim().trim_start_matches([',', ':']).trim().to_string();
            }
        }
    }
    t.to_string()
}

fn speak_to<M: Mouth>(mouth: &Arc<M>, said: &Said, sink: &Sink, s: &str) {
    if s.trim().is_empty() {
        return;
    }
    if let Some(buf) = sink.lock().unwrap().as_mut() {
        buf.push(s.to_string());
        return;
    }
    mouth.say(s);
    let mut q = said.lock().unwrap();
    q.push_back((Instant::now(), s.to_string()));
    while q.len() > 12 {
        q.pop_front();
    }
}

fn emit_if_text(cfg: &Config, tag: &str) {
    if cfg.wait_for_tasks {
        emit_event(tag);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn echo_detection() {
        let mut said = VecDeque::new();
        assert_eq!(short_cmd("alright DEMASCAS, I want you to check what are the latest emails that I have received."),
                   "check what are the latest emails that you have…");
        assert_eq!(short_cmd("I want you to make me a presentation based on water bottle."), "make you a presentation based on water bottle");
        said.push_back((Instant::now(), "Still working on delete notes.txt from my desktop.".to_string()));
        said.push_back((Instant::now(), "You've successfully opened Discord.".to_string()));
        assert!(is_own_echo("you've success opened discord", &said));
        assert!(!is_own_echo("open Safari please", &said));
        assert!(!is_own_echo("hi", &said));
        let mut s2: VecDeque<(Instant, String)> = VecDeque::new();
        for x in ["Alright, Daksh!", "Just tell me what you need, and I'll do my best to help."] {
            s2.push_back((Instant::now(), x.to_string()));
        }
        assert!(!is_own_echo("Daksh, just help.", &s2));
        assert!(is_own_echo_loose("Daksh, just help.", &s2));
        assert!(!is_own_echo_loose("open Safari please", &s2));
    }

    #[test]
    fn answers_to_questions() {
        assert_eq!(answer_value("the file name is, Daksh_Ratthor_CV."), "Daksh_Ratthor_CV");
        assert_eq!(answer_value("it's called budget 2026"), "budget 2026");
        assert_eq!(answer_value("Daksh_Rathore_CV"), "Daksh_Rathore_CV");
    }

    #[test]
    fn file_choices() {
        let c = vec![("SIH 2025.pptx".to_string(), "/a".to_string()), ("budget.xlsx".to_string(), "/b".to_string()), ("notes.txt".to_string(), "/c".to_string())];
        assert_eq!(resolve_choice("the second one", &c).unwrap().1, "/b");
        assert_eq!(resolve_choice("number 3", &c).unwrap().1, "/c");
        assert_eq!(resolve_choice("the last one", &c).unwrap().1, "/c");
        assert_eq!(resolve_choice("the budget", &c).unwrap().1, "/b");
        assert!(resolve_choice("hmm", &c).is_none());
        // live log 18 Nov
        assert!(resolve_choice("I want you to open any random file from the files that you listed.", &c).is_some());
    }

    #[test]
    fn only_stop_gets_through_a_task() {
        for t in ["stop", "DEMASCAS, stop!", "cancel that", "okay stop it", "never mind"] {
            assert!(is_task_stop(t), "{t}");
        }
        for t in ["start kalu", "Who also?", "Happy,", "what are you doing", "don't stop me now, I'm having such a good time"] {
            assert!(!is_task_stop(t), "{t}");
        }
    }

    #[test]
    fn live_log_18_nov() {
        assert_eq!(trailing_stop("- DNG. - Stop!").as_deref(), Some("Stop"));
        assert_eq!(trailing_stop("okay, stop").as_deref(), Some("stop"));
        assert!(trailing_stop("stop the timer for the pasta").is_none());
        assert!(trailing_stop("I told him to just stop, he never listens, stop").is_none());
        assert!(looks_like_file_name("Daksh_Rathore_CV"));
        assert!(looks_like_file_name("it's called budget 2026"));
        for t in ["all right, go and standby for now.", "I don't need you right now, bye.", ", shut down yourself."] {
            assert!(!looks_like_file_name(t), "{t}");
        }
    }
}
