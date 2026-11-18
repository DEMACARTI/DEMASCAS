#!/usr/bin/env python3
"""
DEMASCAS — scripts/exam.py
The JARVIS exam: ~150 real requests, typed into the real daemon (text mode)
with the real model and tool server — but every tool call is recorded and
faked (server/exam_mode.py), so nothing on this computer is touched and no
audio is played.

  ../venv/bin/python scripts/exam.py                 # the whole exam
  ../venv/bin/python scripts/exam.py --only garbled  # one category
  ../venv/bin/python scripts/exam.py --bin core-rs/target/release/demascasd --target rust

Writes docs/exam_runs/<stamp>.json (every turn, with failures explained) and
appends one line to docs/exam_history.md.

Needs: the language model running (the MLX server from run.sh), port 5001
free (DEMASCAS itself must not be running), a built daemon.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import queue
import re
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time

import requests
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
SERVER = "http://127.0.0.1:5001"
TURN_TIMEOUT = 150

# Tools that only read or remember — they don't change anything on the computer.
READ_ONLY = {
    "retrieve_memories", "save_memory", "get_active_window_tree", "list_running_applications",
    "list_open_windows", "find_file", "list_files_in_folder", "get_clipboard", "get_weather",
    "get_news", "search_web", "web_search", "smart_search", "research_web", "web_research", "check_internet",
    "read_webpage", "web_read_article", "scrape_webpage", "web_analyze_page", "web_knowledge",
    "web_scan_for_obstacles", "get_system_info", "wait_for_app_ready", "wait_for_element",
    "browser_get_url", "browser_get_title", "browser_get_page_text", "browser_get_links",
    "browser_get_inputs", "browser_get_dom_summary", "browser_list_tabs", "browser_get_page_html",
    "browser_element_exists", "summarize_latest_pdf", "summarize_selected_pdf",
    "read_current_email", "gmail_read_email", "extract_contact", "architect_plan",
    "learn_app", "guide_app", "watch_topic", "unwatch_topic",
    "set_preference", "remove_preference", "save_user_preference",
    "list_agenda", "list_reminders", "find_contact", "recent_calls", "check_permissions", "list_running_applications", "screen_observe", "screen_find", "form_read", "find_image_by_content", "describe_screen", "look_at_image", "vision_status", "read_notes", "read_mail", "read_screen_text",
    "read_file_text", "morning_brief", "read_chat_messages",
}
# "I've sent it" with no action behind it = a false claim.
CLAIM = re.compile(
    r"\b(i(?:'ve| have)? (?:just )?(?:opened|sent|deleted|created|closed|typed|launched|played|"
    r"saved|moved|switched|set (?:a|the|your) (?:timer|reminder|alarm))|"
    r"(?:message|it) (?:has been |was )?sent|timer (?:is )?set|reminder (?:is )?set|"
    r"^\s*(?:all )?done\b)", re.I)


# ── processes ────────────────────────────────────────────────────────

def port_busy(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def make_home() -> str:
    home = tempfile.mkdtemp(prefix="demascas-exam-")
    os.makedirs(os.path.join(home, ".demascas"))
    real_cache = os.path.expanduser("~/.cache")
    if os.path.isdir(real_cache):
        os.symlink(real_cache, os.path.join(home, ".cache"))   # embedder model, no re-download
    try:
        with open(os.path.expanduser("~/.demascas/config.json")) as f:
            llm = json.load(f).get("llm", {})
    except (OSError, ValueError):
        llm = {}
    llm = {k: v for k, v in llm.items() if "key" not in k.lower()}
    llm["autostart"] = False
    with open(os.path.join(home, ".demascas", "config.json"), "w") as f:
        json.dump({"llm": llm, "web": {"auto_research": False}}, f)
    return home


class Daemon:
    """The daemon in text mode: lines in, @@ events out."""

    def __init__(self, binary: str, env: dict):
        self.p = subprocess.Popen([binary], cwd=ROOT, env=env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, bufsize=1)
        self.q: queue.Queue = queue.Queue()
        self.log: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for raw in self.p.stdout:
            line = raw.rstrip("\n")
            self.log.append(line)
            i = line.find("@@")
            if i >= 0:
                tag, _, text = line[i + 2:].partition(" ")
                self.q.put((tag, text.strip(), time.time()))
        self.q.put(("EOF", "", time.time()))

    def send(self, line: str):
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()

    def until_ready(self, timeout: float = TURN_TIMEOUT):
        """Events up to the next READY → (events, ready_state or None on timeout)."""
        events, deadline = [], time.time() + timeout
        while True:
            try:
                ev = self.q.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                return events, None
            if ev[0] == "READY":
                return events, ev[1]
            if ev[0] == "EOF":
                return events, "EOF"
            events.append(ev)

    def drain(self):
        """Throw away anything left over from the previous item."""
        n = 0
        while True:
            try:
                self.q.get_nowait()
                n += 1
            except queue.Empty:
                return n

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=10)
        except Exception:
            self.p.kill()


def rss_mb(pid: int) -> float:
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout
        return int(out.strip() or 0) / 1024
    except Exception:
        return 0.0


def model_mem_mb() -> float:
    """The model server's memory (top's MEM — RSS undercounts mmap'd weights)."""
    try:
        pids = subprocess.run(["lsof", "-t", "-iTCP:8081", "-sTCP:LISTEN"], capture_output=True,
                              text=True).stdout.split()
        if not pids:
            return 0.0
        out = subprocess.run(["top", "-l", "1", "-pid", pids[0], "-stats", "mem"],
                             capture_output=True, text=True).stdout.strip().splitlines()[-1]
        m = re.match(r"([\d.]+)([KMG])", out.strip())
        if not m:
            return 0.0
        return float(m.group(1)) * {"K": 1 / 1024, "M": 1, "G": 1024}[m.group(2)]
    except Exception:
        return 0.0


# ── scoring ──────────────────────────────────────────────────────────

TRACE_MARKS = ("[ROUTE]", "[📋", "[CONFIRM]", "[✂ SPLIT]", "[↪ ANSWER]", "[⏹ CANCEL]", "[🔧 TASK]", "[▶ NEXT]")


def check_plan(exp: dict, said: str, calls: list[dict], trace: list[str]) -> list[str]:
    """Planner expectations: did it plan, how many steps, tools in order,
    re-plans, and an honest ending when a step failed."""
    why = []
    planned = any("[📋 STEP" in l or "[📋 PLAN]" in l for l in trace) or any("[ROUTE] Plan" in l for l in trace)
    if "plan" in exp and bool(exp["plan"]) != planned:
        why.append(f"planner {'not used' if exp['plan'] else 'used when it should not be'}")
    n = 0
    for l in trace:
        m = re.search(r"\[📋 STEP \d+/(\d+)\]", l)
        if m:
            n = max(n, int(m.group(1)))
    if "steps" in exp and planned:
        lo, hi = exp["steps"]
        if not lo <= n <= hi:
            why.append(f"plan had {n} steps (expected {lo}–{hi})")
    if exp.get("step_tools_in_order"):
        names = [c["name"] for c in calls]
        pos = 0
        for t in exp["step_tools_in_order"]:
            alts = t if isinstance(t, list) else [t]
            try:
                pos = next(i for i in range(pos, len(names)) if names[i] in alts) + 1
            except StopIteration:
                why.append(f"tools out of order / missing: wanted {exp['step_tools_in_order']}, got {names}")
                break
    replans = sum(1 for l in trace if "[📋 PLAN]" in l and any("STEP FAILED" in x for x in trace[:trace.index(l)]))
    if "replans" in exp and replans != exp["replans"]:
        why.append(f"{replans} re-plans (expected {exp['replans']})")
    if exp.get("honest_end") and any("STEP FAILED" in l for l in trace):
        if not re.search(r"couldn't|could not|didn't|wasn't able|but |failed", said, re.I):
            why.append("a step failed but the ending didn't say so")
    return why

def check_turn(exp: dict, said: str, calls: list[dict], ready: str | None) -> list[str]:
    """Why this turn failed ([] = passed)."""
    why = []
    names = [c["name"] for c in calls]
    actions = [n for n in names if n not in READ_ONLY]
    blob = {n: " ".join(json.dumps(c["args"]).lower() for c in calls if c["name"] == n) for n in set(names)}

    if ready is None:
        return ["timed out"]
    for t in exp.get("tools", []):
        if t not in names:
            why.append(f"did not call {t}")
    if exp.get("any_tool") and not any(t in names for t in exp["any_tool"]):
        why.append(f"called none of {exp['any_tool']}")
    for t, pat in (exp.get("args") or {}).items():
        if t in blob and not re.search(pat, blob[t], re.I):
            why.append(f"{t} args don't match /{pat}/: {blob[t][:120]}")
    for t in exp.get("forbid", []):
        if t in names:
            why.append(f"must not call {t}")
    if exp.get("no_action") and actions:
        why.append(f"acted when it shouldn't: {actions}")
    if exp.get("only"):
        allowed = set(exp.get("tools", [])) | set(exp.get("any_tool", []))
        extra = [n for n in actions if n not in allowed]
        if extra:
            why.append(f"did extra things nobody asked for: {extra}")
    for t in exp.get("confirmed", []):
        if not any(c["name"] == t and c["confirmed"] for c in calls):
            why.append(f"{t} not run after a yes")
    if exp.get("asks") and "?" not in said:
        why.append("should have asked a question")
    if exp.get("silent") and said.strip():
        why.append(f"should have stayed quiet, said: {said[:100]}")
    if exp.get("speaks") and not said.strip():
        why.append("said nothing")
    if exp.get("sleeps") and ready != "SLEEPING":
        why.append("did not go to sleep")
    for pat in exp.get("say", []):
        if not re.search(pat, said, re.I):
            why.append(f"reply lacks /{pat}/")
    if exp.get("say_any") and not any(re.search(p, said, re.I) for p in exp["say_any"]):
        why.append(f"reply has none of {exp['say_any']}")
    for pat in exp.get("say_not", []):
        if re.search(pat, said, re.I):
            why.append(f"reply must not say /{pat}/")
    if not actions and CLAIM.search(said) and not exp.get("allow_claim"):
        why.append(f"claimed an action it didn't do: \"{CLAIM.search(said).group(0)}\"")
    if exp.get("max_s") and exp.get("_seconds", 0) > exp["max_s"]:
        why.append(f"too slow: {exp['_seconds']:.1f} s > {exp['max_s']} s")
    return why


def run_item(d: Daemon, item: dict, state: str) -> tuple[dict, str]:
    requests.post(f"{SERVER}/exam/reset", json=item.get("world", {}), timeout=5)
    d.send("/reset")
    _, state = d.until_ready(60)
    time.sleep(0.2)
    d.drain()                         # nothing from the last item may leak into this one
    requests.get(f"{SERVER}/exam/calls", timeout=5)
    turns_out, ok = [], True
    for i, turn in enumerate(item["turns"]):
        if turn.get("reset") and i > 0:
            d.send("/reset")
            _, state = d.until_ready(60)
            requests.get(f"{SERVER}/exam/calls", timeout=5)
        if state == "SLEEPING" and turn.get("wake", True):
            d.send("/wake")
            _, state = d.until_ready(30)
        line = turn["say"] if "conf" not in turn else f"~{turn['conf']}|{turn['say']}"
        log_from = len(d.log)
        t0 = time.time()
        d.send(line)
        if turn.get("during"):
            # said WHILE the task is running ("what are you doing?", "stop")
            time.sleep(float(turn.get("after_s", 2)))
            for extra in ([turn["during"]] if isinstance(turn["during"], str) else turn["during"]):
                d.send(extra)
                time.sleep(float(turn.get("gap_s", 1.5)))
        events, ready = d.until_ready()
        if turn.get("during"):
            # if the extra line was read as its own turn, collect that turn too
            while True:
                more, r2 = d.until_ready(4)
                if r2 is None:
                    break
                events += more
                ready = r2
        t_done = time.time() - t0
        says = [e for e in events if e[0] == "SAY"]
        said = " ".join(e[1] for e in says)
        first = (says[0][2] - t0) if says else None
        calls = requests.get(f"{SERVER}/exam/calls", timeout=5).json()["calls"]
        exp = dict(turn.get("expect", {}))
        exp["_seconds"] = t_done
        why = check_turn(exp, said, calls, ready)
        why += check_plan(exp, said, calls, [l for l in d.log[log_from:] if any(k in l for k in TRACE_MARKS)])
        llm = [l.split("[LLM] ", 1)[1] for l in d.log[log_from:] if "[LLM] " in l]
        trace = [l.strip() for l in d.log[log_from:] if any(k in l for k in TRACE_MARKS)]
        turns_out.append({"say": turn["say"], "conf": turn.get("conf"), "said": said,
                          "tools": [{"name": c["name"], "args": c["args"], "confirmed": c["confirmed"]}
                                    for c in calls],
                          "first_say_s": round(first, 2) if first is not None else None,
                          "done_s": round(t_done, 2), "llm_calls": llm, "trace": trace, "fail": why,
                          "log": d.log[log_from:][-60:] if why else []})
        ok = ok and not why
        state = ready or state
        if ready is None:          # stuck — the caller restarts the daemon
            break
    return {"id": item["id"], "cat": item["cat"], "pass": ok, "turns": turns_out}, state


# ── main ─────────────────────────────────────────────────────────────

def load_items(only: str | None, ids: str | None, files: str | None = None) -> list[dict]:
    items = []
    for path in sorted(glob.glob(os.path.join(ROOT, "tests", "exam", "*.yaml"))):
        if files and not any(f in os.path.basename(path) for f in files.split(",")):
            continue
        with open(path) as f:
            for it in yaml.safe_load(f) or []:
                it.setdefault("cat", re.sub(r"^\d+_", "", os.path.splitext(os.path.basename(path))[0]))
                if "say" in it:                      # one-turn shorthand
                    it["turns"] = [{"say": it.pop("say"), "expect": it.pop("expect", {}),
                                    **({"conf": it.pop("conf")} if "conf" in it else {})}]
                items.append(it)
    if only:
        cats = set(only.split(","))
        items = [i for i in items if i["cat"] in cats]
    if ids:
        want = set(ids.split(","))
        items = [i for i in items if i["id"] in want]
    return items


def pct(xs, q):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    return round(xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))], 2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", default=os.path.join(ROOT, "core-rs", "target", "release", "demascasd"))
    ap.add_argument("--target", default="cpp", help="label for the history (cpp / rust)")
    ap.add_argument("--only", help="comma-separated categories")
    ap.add_argument("--ids", help="comma-separated item ids")
    ap.add_argument("--files", help="comma-separated exam file name parts (e.g. t1_light)")
    ap.add_argument("--note", default="")
    a = ap.parse_args()

    items = load_items(a.only, a.ids, a.files)
    if not items:
        print("No exam items found.")
        return 2
    if port_busy(5001):
        print("Port 5001 is in use — stop DEMASCAS (or the other tool server) first.")
        return 2
    if not port_busy(8081):
        print("The language model isn't running on :8081 — start it (./scripts/run.sh starts MLX).")
        return 2

    home = make_home()
    env = dict(os.environ, HOME=home, DEMASCAS_EXAM="1", DEMASCAS_SKIP_MCP_BOOT="1",
               DEMASCAS_TEXT_MODE="1", PYTHONUNBUFFERED="1")
    srv_log = open(os.path.join(home, "server.log"), "w")
    srv = subprocess.Popen([PY, "main.py"], cwd=ROOT, env=env, stdout=srv_log, stderr=subprocess.STDOUT)
    try:
        for _ in range(90):
            try:
                if requests.get(f"{SERVER}/health", timeout=1).ok:
                    break
            except requests.RequestException:
                time.sleep(1)
        else:
            print("Tool server didn't start; see", srv_log.name)
            return 2
        requests.post(f"{SERVER}/exam/reset", json={}, timeout=5).raise_for_status()
        try:
            model = requests.get(f"{SERVER}/llm", timeout=5).json().get("model", "?")
        except Exception:
            model = "?"

        d = Daemon(a.bin, env)
        _, state = d.until_ready(120)
        peak = {"daemon": 0.0, "server": 0.0}
        stop = threading.Event()

        def sample():
            while not stop.is_set():
                peak["daemon"] = max(peak["daemon"], rss_mb(d.p.pid))
                peak["server"] = max(peak["server"], rss_mb(srv.pid))
                time.sleep(0.5)
        threading.Thread(target=sample, daemon=True).start()

        results, t_start = [], time.time()
        aborted = ""
        for n, item in enumerate(items, 1):
            if not port_busy(8081):
                # Results without a model are meaningless — stop instead of
                # recording a tail of bogus failures.
                aborted = f"model server stopped before item {n}/{len(items)}"
                print(f"\n[!] {aborted} — run aborted; not written to the history.")
                break
            r, state = run_item(d, item, state or "SLEEPING")
            results.append(r)
            mark = "✓" if r["pass"] else "✗"
            first = r["turns"][0]
            print(f"{mark} {n:3d}/{len(items)} [{r['cat']:12s}] {item['id']:28s} "
                  f"{first['done_s']:5.1f}s  {('— ' + '; '.join(t for x in r['turns'] for t in x['fail']))[:150] if not r['pass'] else ''}",
                  flush=True)
            if any(t["fail"] == ["timed out"] for t in r["turns"]):
                d.p.kill()
                d = Daemon(a.bin, env)
                _, state = d.until_ready(120)
        stop.set()
        warm = [l.split("] ", 1)[-1] for l in d.log if "warm-up done" in l]
        if warm:
            print(f"[warm-up] {warm[-1]}")
        d.close()
        model_mb = model_mem_mb()
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except subprocess.TimeoutExpired:
            srv.kill()
        srv_log.close()

    if aborted:
        shutil.rmtree(home, ignore_errors=True)
        return 3

    # ── report
    cats: dict[str, list] = {}
    for r in results:
        cats.setdefault(r["cat"], []).append(r["pass"])
    turns = [t for r in results for t in r["turns"]]
    firsts = [t["first_say_s"] for t in turns]
    dones = [t["done_s"] for t in turns]
    passed = sum(r["pass"] for r in results)
    summary = {
        "when": time.strftime("%Y-%m-%d %H:%M"), "target": a.target, "model": model,
        "passed": passed, "total": len(results), "pct": round(100 * passed / len(results), 1),
        "by_category": {c: f"{sum(v)}/{len(v)}" for c, v in sorted(cats.items())},
        "first_say_s": {"median": pct(firsts, 0.5), "p90": pct(firsts, 0.9)},
        "done_s": {"median": pct(dones, 0.5), "p90": pct(dones, 0.9)},
        "peak_mb": {"daemon": round(peak["daemon"]), "tool_server": round(peak["server"]),
                    "model": round(model_mb)},
        "minutes": round((time.time() - t_start) / 60, 1), "note": a.note,
    }
    print("\n" + json.dumps(summary, indent=1))
    out_dir = os.path.join(ROOT, "docs", "exam_runs")
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M")
    with open(os.path.join(out_dir, f"{stamp}-{a.target}.json"), "w") as f:
        json.dump({"summary": summary, "results": results}, f, indent=1, ensure_ascii=False)
    hist = os.path.join(ROOT, "docs", "exam_history.md")
    new = not os.path.exists(hist)
    with open(hist, "a") as f:
        if new:
            f.write("# JARVIS exam history\n\nEach row is one run of `scripts/exam.py` "
                    "(real model + daemon, every tool call faked).\n\n"
                    "| When | Daemon | Model | Pass | By category | First word (median / p90) | "
                    "Turn done (median / p90) | Peak RAM daemon / server / model | Note |\n"
                    "|---|---|---|---|---|---|---|---|---|\n")
        bc = ", ".join(f"{c} {v}" for c, v in summary["by_category"].items())
        f.write(f"| {summary['when']} | {a.target} | {model.split('/')[-1]} | "
                f"{passed}/{len(results)} ({summary['pct']}%) | {bc} | "
                f"{summary['first_say_s']['median']} / {summary['first_say_s']['p90']} s | "
                f"{summary['done_s']['median']} / {summary['done_s']['p90']} s | "
                f"{summary['peak_mb']['daemon']} / {summary['peak_mb']['tool_server']} / "
                f"{summary['peak_mb']['model']} MB | {a.files or a.only or 'all'}{(' — ' + a.note) if a.note else ''} |\n")
    shutil.rmtree(home, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
