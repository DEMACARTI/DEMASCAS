# DEMASCAS test report — 17 November 2026

**What was tested:** everything built so far, in three difficulty tiers, from instant reflexes to multi-step tasks that need the model to understand what's going on. The **implementation planner** got special attention. Every test went through the real Rust daemon, the real tool server and the real model (Qwen2.5-3B-4bit on MLX); actions were faked so nothing on the Mac changed. A short **real, read-only pass** ran on the Mac itself.

## Results at a glance

| Layer | First run | After fixes |
|---|---|---|
| Rust unit tests | 84 → 85 pass | 85/85 |
| Python unit tests | 66 pass | 66/66 |
| **Tier 1 — light** (45 items: apps, keys, clock, web, files, Apple apps, rules, hearing) | 42/45 (3 were my test mistakes) | **45/45** |
| **Tier 2 — medium** (29 items: knowledge, live info, reading, messaging, follow-ups, verification, injection, confirmations) | 25/29 | **29/29** |
| **Tier 3 — intelligent** (25 items: plans, reasoning, goals, pause/resume, failures, injection, live interruption, "should not plan") | 9/25 → 18/25 | **25/25** |
| Full regression (all 275 items, old + new) | 271/275 | 274/275; the one left needs the document index (not built) |
| Real read-only pass on the Mac | 8/8 + 1 skipped | — |

**Speed (Tier runs):**

| Tier | First word, median / slowest 10% | Whole task, median / slowest 10% |
|---|---|---|
| Tier 1 | 0.01 / 0.03 s | — |
| Tier 2 | 1.25 / 3.46 s | — |
| Tier 3 | 0.02 / 2.08 s | 1.17 / 6.04 s |

**Memory:** daemon 17–18 MB, tool server 210–370 MB, model about 2.3–2.7 GB.

## The planner — does it work?

**Yes, and the important finding is *why* it works.** In the final Tier 3 run, plans were made like this:

| How the plan was made | Items |
|---|---|
| Split from the sentence itself, no model | 8 |
| Built-in template ("prepare for my meeting with Aman" → agenda, mail, chats, what to know) | 1 |
| The 3B model's own plan | 2 ("organise my downloads"; the re-plan after a failed presentation) |
| Correctly *not* planned (quick recipes, split requests, answers) | 14 |

**What the planner checks showed:**
- **The steps are right and in the right order** on every explicit multi-step task: report → summary → email, research → presentation, find → open → read, CV → note, calendar → reminder at the right time, page → summary email.
- **Pause and resume works:** a plan that reaches a message waits for "yes", sends, then carries on. "No" stops it.
- **Failure is handled:**
  - a failed step is re-planned once;
  - a step that needs information only you have stops and asks ("what's the exact file name?");
  - a plan that had a failure says so at the end instead of "Done."
- **Speed:** model plans take 0.8–1.3 s since the prompt became short and cached; the first version took 12–22 s.
- **The weakness:**
  - On its own, the 3B model is a poor planner. In the first runs it returned one-step plans for three-step requests, planned "make a presentation" for "prepare for my meeting", and invented five junk steps in a re-plan.
  - The planner is reliable because the brain splits sentences, uses templates and does arithmetic in code, and leaves the model only the vague goals.
  - Vague goals are still weak. "organise my downloads" ended up opening files rather than proposing an organisation.

## Problems found during testing — and why

### Safety (fixed)
1. **An email was "sent" with no confirmation.**
   - "Write an email to Aman telling him…" fell outside the email recipe because of the phrase "telling him". The model then typed into a browser and called `type_and_submit`.
   - That tool was only gated inside chat and Mail apps, not in a browser.
   - **Fix:** `type_and_submit` always needs a spoken yes. The email recipe understands "telling / letting him know".
2. **The next queued step cancelled a message.**
   - In "message Hatim…, then find my CV", the queued part ran while the "Send 'I'm on my way'?" question was still open. It was taken as an unclear answer, which cancels.
   - **Fix:** queued work waits until the question is answered.

### Planner and brain (fixed)
3. **One recipe swallowed a whole multi-part sentence.** "check my calendar… and remind me…" matched the calendar recipe; "message Hatim… then find my CV…" matched the message recipe. **Fix:** a sentence with several parts goes to the planner.
4. **"Summarise my CV" with no file known yet:** the model researched "how to summarise a CV" on the web. **Fix:** a new file action, find → read → summarise.
5. **Results didn't flow between split parts:** the email went to "aman a summary" with no content. **Fix:** later parts that refer back ("it", "a summary") make it a plan; "email X a summary" is parsed correctly; the email gets the previous step's text.
6. **The app was carried onto unrelated parts:** "find my CV **on Discord**", "add milk **on Safari**" (after "close Safari"). **Fix:** the app is carried only from open/switch parts, and only onto chat or screen actions.
7. **"Remind me 30 minutes before the first meeting":** the model can't do that arithmetic. In a test I also found an agenda-parsing bug that read "2 things: 10 AM" as hour 210. **Fix:** computed in code from the agenda; parser fixed.
8. **Re-plans repeated finished steps or answered their own questions.** **Fix:** completed steps are filtered out; question-failures ask you; endings are honest.
9. **Slow recipes blocked the ears.** A presentation or research ran inline, so "what are you doing?" and "stop" couldn't be heard until it finished. **Fix:** slow recipes run in the background. The status and stop tests now pass.
10. **A bare "Done." in reply to information requests** ("compare…", repeated lookups). **Fix:** "compare" counts as wanting information; repeats return the result already fetched.

### Hearing and recipes (fixed)
11. **"actually open the finder app"** was ignored as side talk. "Actually" wasn't treated as a filler word, and a command arriving during a question wasn't marked as addressed.
12. **Missing phrasings:** "look up the weather…", "get me ready for the day", "type X", "type X and press enter".
13. **"What was the last thing I asked?" was flaky:** the model sometimes ignored the request log. It's now answered from the log in code.

### Found on the real Mac (fixed / explained)
14. **"Read the screen" read Finder's menu bar** ("About This Mac…"). Menus are now skipped. Finder with no window open still has nothing useful to read.
15. **File search picked "Daksh_Final_CV(1).pdf"** over the original. Originals now rank first.
16. **Calendar 4.1 s / Reminders 13.8 s on first use** turned out to be macOS's one-time permission prompts. Warm reads: **0.02 s / 0.72 s** (cold), then instant.
17. **Mail** was skipped: it wasn't open, and reading it would have launched it.

### Problems with the tests themselves (not DEMASCAS)
- **Harness out of step.** A line typed while a slow inline task ran was read later as its own turn, so the rest of that tier was scored against the wrong replies. That invalidated about 7 Tier 3 results in the first run. Fixed: the harness drains leftovers and absorbs extra turns.
- **8 expectations were wrong:** timer statements flagged as false claims, a regex expecting the wrong argument order, "ありがとう" when I expected romaji, a correct RAM/storage answer, compare-as-plan, the pause test's expected shape, and the ambiguous case.
- **A real issue hidden in one of them:** "ありがとう" in Japanese script can't be spoken by the English Kokoro voice. Spoken replies should transliterate.

## What works well
- **Instant, exact handling of everyday requests:** apps, keys, timers, web, files, Apple apps.
- **Every safety gate held:**
  - messages, deletes, risky keys and calendar additions all ask first;
  - garbled or unclear speech never acts;
  - injected instructions in pages, mail and search results were never followed;
  - verification never let a failed action be reported as done.
- **Reading and summarising:** pages, files, chats, mail, the brief.
- **Multi-step tasks:** explicit ones are planned correctly, with pause/resume and honest endings.

## Still fragile / not covered
1. **Vague goals rely on the 3B model** ("organise my downloads", "prepare for X" beyond the template). A bigger model or more templates are needed.
2. **The document index (Phase E)** isn't built ("what does my report say about the budget").
3. **Plans repeat a file search** when a later step opens or reads "it". The found file should be reused.
4. **Voice-only behaviour isn't in the exam:** wake word, barge-in, echo cancelling, joining trailing sentences. iMessage needs Full Disk Access and your number in `config.json`. These need your live check.
5. **Click verification** adds about 0.8 s per click (the screen fingerprint).
6. **Spoken replies in non-Latin scripts.**

## Ranked fix list for the next round
1. Reuse the found file across plan steps (no duplicate searches).
2. More plan templates plus a "propose, don't act" mode for vague organising goals; model plans checked against the request.
3. Transliterate non-Latin text before speaking.
4. Make the click-verification fingerprint cheaper (fewer nodes, the target window only).
5. Phase E: the document index and `ask_my_files`.
6. A live voice + iMessage checklist session with you.

*Run files:*
- `docs/exam_runs/*.json` — every turn with its route, planner trace, tools and timings;
- `docs/exam_history.md` — one row per run;
- `docs/exam_runs/real_readonly_*.json` — the real pass.
