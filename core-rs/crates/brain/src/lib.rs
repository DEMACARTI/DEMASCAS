//! DEMASCAS's one brain.  Replaces the three brains (C++ tool loop, Python
//! agents' decision loops, skills dispatch) and the five routers.
//!
//! * [`utterance`] — what kind of thing was said (yes/no, stop, status,
//!   complaint, wake word, recogniser junk) — pure, instant, no model.
//! * [`shortcuts`] — whole-request keyboard shortcuts ("undo", "new tab").
//! * [`reflex`] — one exact action with no model (open/close apps, time,
//!   date, weather, "remember that…", learning an app, shortcuts).
//! * [`router`] — the single decision: ignore / control / clarify / reflex /
//!   skill / answer / look up (read-only) / act / agent.

//! * [`prompts`], [`actions`] — the model's instructions and reading its output.
//! * [`engine`] — runs a decision: reflex, skill, chat, the one tool loop,
//!   agents; applies the policy (confirmations are always asked aloud).

pub mod actions;
pub mod engine;
pub mod governance;
pub mod normalize;
pub mod planner;
pub mod prompts;
pub mod reflex;
pub mod router;
pub mod shortcuts;
pub mod utterance;
