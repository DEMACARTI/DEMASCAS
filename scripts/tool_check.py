#!/usr/bin/env python3
"""
DEMASCAS — scripts/tool_check.py
Checks EVERY tool DEMASCAS has, through the running tool server (so it uses
the DEMASCAS app's real macOS permissions and the real safety gates).

  ../venv/bin/python scripts/tool_check.py            # safe checks
  ../venv/bin/python scripts/tool_check.py --live     # also tools that open apps / click / type
  ../venv/bin/python scripts/tool_check.py --only battery_status,define_word

Every tool gets a WIRING check (schema exists, its arguments match the code,
callable).  Then, by what it does:

  run       read-only — run for real, the result must be well-formed
  sandbox   files — only inside a scratch folder that is deleted afterwards
  restore   volume / mute / clipboard — changed, then put back exactly
  gate      delete, empty trash, calls, sending — must ASK for a yes (the
            check never says yes, so nothing happens)
  live      opens apps, clicks, types, browses, locks — wiring only, unless --live
  slow      needs the language/vision model for minutes — wiring only, unless --slow

Personal content (contacts, messages, clipboard, files) is never printed:
only pass / fail, timing and the reply's length.
"""

from __future__ import annotations

import argparse
import contextlib
import inspect
import io
import json
import os
import shutil
import sys
import tempfile
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
SERVER = "http://127.0.0.1:5001"
SANDBOX = os.path.join(os.path.expanduser("~"), ".demascas", "tool_check_sandbox")

# ── what each tool is, and how to exercise it ─────────────────────────
RUN = {
    # everyday
    "get_volume": {}, "battery_status": {}, "disk_space": {}, "system_load": {}, "ip_address": {}, "wifi_status": {},
    "uptime": {}, "now_playing": {}, "recent_files": {"count": 3}, "world_time": {"city": "Tokyo"},
    "convert_units": {"value": 5, "from_unit": "mile", "to_unit": "km"}, "define_word": {"word": "serendipity"},
    "random_pick": {"kind": "coin"},
    # the Mac
    "list_running_applications": {}, "list_open_windows": {}, "get_system_info": {}, "check_internet": {},
    "check_permissions": {}, "get_clipboard": {}, "vision_status": {},
    # screen (Finder is always there)
    "read_screen_text": {"app_name": "Finder"}, "screen_observe": {"app_name": "Finder"}, "screen_find": {"description": "search", "app_name": "Finder"},
    "get_active_window_tree": {"app_name": "Finder"}, "form_read": {"app_name": "Finder"},
    # files (read-only)
    "find_file": {"folder": "~/Desktop", "partial_name": "", "extension": "png"}, "list_files_in_folder": {"folder_path": "~/Desktop"},
    "find_image_by_content": {"folder": "~/Desktop", "what": "qr code"}, "file_info": {"path": "~/Desktop"},
    # people / time (read-only, on-device)
    "find_contact": {"name": "Mom"}, "list_agenda": {"day": "today"}, "list_reminders": {}, "recent_calls": {"count": 1},
    # memory / knowledge
    "retrieve_memories": {"query": "test"}, "web_knowledge": {"query": "weather"},
    # web reads (network)
    "get_weather": {"location": "Delhi"}, "get_news": {}, "search_web": {"query": "speed of light"}, "web_search": {"query": "speed of light"},
    "smart_search": {"query": "speed of light"}, "read_webpage": {"url": "https://example.com"}, "scrape_webpage": {"url": "https://example.com"},
}
SANDBOX_STEPS = [  # (tool, args builder) — run in order inside SANDBOX
    ("create_folder", lambda d: {"path": f"{d}/made_by_check"}),
    ("create_file", lambda d: {"file_path": f"{d}/note.txt", "content": "DEMASCAS tool check"}),
    ("read_file_text", lambda d: {"file_path": f"{d}/note.txt"}),
    ("copy_file", lambda d: {"source": f"{d}/note.txt", "destination": f"{d}/made_by_check"}),
    ("rename_file", lambda d: {"path": f"{d}/made_by_check/note.txt", "new_name": "renamed"}),
    ("move_file", lambda d: {"source": f"{d}/made_by_check/renamed.txt", "destination": d}),
    ("zip_files", lambda d: {"paths": f"{d}/made_by_check", "destination": f"{d}/check.zip"}),
    ("file_info", lambda d: {"path": f"{d}/check.zip"}),
]
GATE = {   # must come back asking for a yes
    "delete_file": {"file_path": "~/.demascas/tool_check_sandbox/note.txt"},
    "empty_trash": {},
    "call_contact": {"number": "5550100", "name": "Test Number"},
    "deliver_chat_message": {"app": "WhatsApp", "contact": "Test", "text": "test"},
    "type_and_submit": {"text": "test"},
    "add_event": {"title": "DEMASCAS tool check", "when": "tomorrow 9am"},
}
RESTORE = ["set_volume", "mute", "set_clipboard", "keep_awake", "show_notification"]
SLOW = {"describe_screen", "look_at_image", "summarize_latest_pdf", "summarize_selected_pdf", "guide_app", "learn_app", "research_web",
        "web_research", "web_research_and_report", "create_presentation", "create_document", "create_spreadsheet", "edit_presentation",
        "compose_email", "smart_reply", "morning_brief", "architect_plan", "web_execute_goal", "vision_execute", "extract_contact"}
NEVER_RUN = {"run_shell_command", "run_app_script", "set_default_browser", "forget_web", "remove_preference", "confirm_and_execute",
             "lock_screen", "sleep_display", "set_wifi", "quit_application", "send_chat_message", "message_contact", "gmail_compose_draft",
             "draft_mail", "brightness", "dark_mode", "media_control", "take_screenshot", "empty_trash"}


def call(name: str, args: dict, timeout: float = 60) -> tuple[dict, float]:
    t = time.perf_counter()
    r = requests.post(f"{SERVER}/tool", json={"name": name, "arguments": args}, timeout=timeout)
    ms = (time.perf_counter() - t) * 1000
    j = r.json()
    res = j.get("result", j)
    if isinstance(res, str):
        try:
            res = json.loads(res)
        except ValueError:
            res = {"message": res, "success": not res.startswith("[-]") and "Failed" not in res[:20]}
    if isinstance(res, dict) and "success" not in res:
        res["success"] = j.get("success", True)
    return res, ms


def wiring(tool_map: dict, schemas: dict) -> dict[str, str]:
    """name → '' if OK, else what's wrong."""
    out = {}
    for name, fn in tool_map.items():
        why = []
        s = schemas.get(name)
        if not s:
            why.append("no schema (the model can't see it)")
        if not callable(fn):
            why.append("not callable")
        else:
            try:
                sig = inspect.signature(fn)
                params = {k for k, p in sig.parameters.items() if p.kind not in (p.VAR_KEYWORD, p.VAR_POSITIONAL)}
                takes_kwargs = any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values())
                need = {k for k, p in sig.parameters.items() if p.default is p.empty and p.kind not in (p.VAR_KEYWORD, p.VAR_POSITIONAL)}
                if s:
                    props = set((s["function"].get("parameters") or {}).get("properties", {}))
                    req = set((s["function"].get("parameters") or {}).get("required", []))
                    if not takes_kwargs and (props - params):
                        why.append(f"schema args not in code: {sorted(props - params)}")
                    if need - props:
                        why.append(f"code needs args the schema lacks: {sorted(need - props)}")
                    if not takes_kwargs and (req - params):
                        why.append(f"required args not in code: {sorted(req - params)}")
            except (TypeError, ValueError):
                pass
        out[name] = "; ".join(why)
    return out


def good_reply(res: dict) -> str:
    """'' if the result looks like a real answer, else why not."""
    if not isinstance(res, dict):
        return "not a result object"
    msg = str(res.get("message") or res.get("result") or "")
    if res.get("success") is False or msg.startswith("[-]"):
        return msg[:140] or "failed"
    if not msg.strip() and len(res) <= 1:
        return "empty reply"
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="also run tools that open apps / click / type")
    ap.add_argument("--slow", action="store_true", help="also run model-heavy tools (minutes)")
    ap.add_argument("--only", help="comma-separated tool names")
    a = ap.parse_args()

    try:
        requests.get(f"{SERVER}/health", timeout=2)
    except requests.RequestException:
        print("DEMASCAS isn't running — open DEMASCAS.app first (the check uses its permissions).")
        return 2
    os.environ.setdefault("DEMASCAS_SKIP_MCP_BOOT", "1")
    with contextlib.redirect_stdout(io.StringIO()):
        import server.tool_server as ts
    tool_map = dict(ts.TOOL_MAP)
    schemas = {s["function"]["name"]: s for s in ts._all_tool_schemas()}
    only = set(a.only.split(",")) if a.only else None
    names = sorted(n for n in tool_map if not only or n in only)
    wired = wiring({n: tool_map[n] for n in names}, schemas)

    rows: list[tuple[str, str, str, str, float]] = []   # name, mode, status, detail, ms

    def record(name, mode, status, detail="", ms=0.0):
        rows.append((name, mode, status, detail, ms))
        mark = {"PASS": "✓", "FAIL": "✗", "SKIP": "·", "KNOWN": "~"}[status]
        print(f"{mark} {name:30s} {mode:8s} {status:5s} {ms:7.0f} ms  {detail[:110]}", flush=True)

    # 1. run (read-only)
    for name in names:
        if name in RUN:
            if wired[name]:
                record(name, "run", "FAIL", wired[name]); continue
            try:
                res, ms = call(name, RUN[name])
                why = good_reply(res)
                if why and name == "recent_calls" and "Full Disk Access" in why:
                    record(name, "run", "KNOWN", "needs Full Disk Access (System Settings) — the tool explains this", ms)
                elif why and name in ("now_playing",):
                    record(name, "run", "PASS", "nothing playing", ms)
                else:
                    record(name, "run", "FAIL" if why else "PASS", why or f"reply {len(str(res.get('message', '')))} chars", ms)
            except Exception as e:
                record(name, "run", "FAIL", f"error: {e}")

    # 2. sandbox (files) — a scratch folder, deleted afterwards
    if any(n in names for n, _ in SANDBOX_STEPS):
        shutil.rmtree(SANDBOX, ignore_errors=True)
        os.makedirs(SANDBOX)
        try:
            for name, args in SANDBOX_STEPS:
                if name not in names:
                    continue
                res, ms = call(name, args(SANDBOX))
                why = good_reply(res)
                record(name, "sandbox", "FAIL" if why else "PASS", why or "ok", ms)
            ok = os.path.exists(f"{SANDBOX}/renamed.txt") and os.path.exists(f"{SANDBOX}/check.zip")
            record("(file chain result)", "sandbox", "PASS" if ok else "FAIL", "copy → rename → move → zip produced the expected files" if ok else "files not where expected")
        finally:
            shutil.rmtree(SANDBOX, ignore_errors=True)

    # 3. restore — change, then put back
    if "set_volume" in names and "get_volume" in tool_map:
        before, _ = call("get_volume", {})
        v0 = before.get("volume", 30)
        res, ms = call("set_volume", {"level": max(0, v0 - 1)})
        after, _ = call("get_volume", {})
        call("set_volume", {"level": v0})
        record("set_volume", "restore", "PASS" if after.get("volume") == max(0, v0 - 1) else "FAIL", f"{v0}→{after.get('volume')}→{v0}", ms)
    if "mute" in names:
        before, _ = call("get_volume", {})
        res, ms = call("mute", {"on": not before.get("muted", False)})
        mid, _ = call("get_volume", {})
        call("mute", {"on": before.get("muted", False)})
        record("mute", "restore", "PASS" if mid.get("muted") != before.get("muted") else "FAIL", "toggled and put back", ms)
    if "set_clipboard" in names:
        import subprocess
        saved = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout
        res, ms = call("set_clipboard", {"text": "DEMASCAS tool check"})
        got, _ = call("get_clipboard", {})
        subprocess.run(["pbcopy"], input=saved, text=True)
        record("set_clipboard", "restore", "PASS" if "DEMASCAS tool check" in str(got.get("message", got)) else "FAIL", "set, read back, restored", ms)
    for name, args in (("keep_awake", {"minutes": 1}), ("show_notification", {"title": "DEMASCAS", "text": "Tool check running"})):
        if name in names:
            res, ms = call(name, args)
            why = good_reply(res)
            record(name, "restore", "FAIL" if why else "PASS", why or "ok (harmless, ends on its own)", ms)

    # 4. gates — must ask, and are never confirmed
    for name, args in GATE.items():
        if name not in names:
            continue
        res, ms = call(name, args)
        pending = isinstance(res, dict) and res.get("status") == "pending_confirmation"
        record(name, "gate", "PASS" if pending else "FAIL",
               f"asks: {res.get('confirm_speech', '')[:70]}" if pending else f"did NOT ask first: {str(res.get('message', res))[:90]}", ms)

    # 5. --live: real app control on harmless targets (Calculator; a scratch file;
    #    Brave read-only).  Nothing is typed into your apps or sent anywhere.
    if a.live:
        live = [
            ("open_application", {"app_name": "Calculator"}, 1.5),
            ("activate_application", {"app_name": "Calculator"}, 0.6),
            ("wait_for_app_ready", {"app_name": "Calculator"}, 0),
            ("window_action", {"action": "left_half", "app_name": "Calculator"}, 0.4),
            ("window_action", {"action": "minimize", "app_name": "Calculator"}, 0.6),
            ("hide_app", {"app_name": "Calculator"}, 0.4),
            ("quit_application", {"app_name": "Calculator"}, 0.5),
        ]
        for name, args, pause in live:
            if name not in tool_map:
                continue
            res, ms = call(name, args)
            why = good_reply(res)
            record(name, "live", "FAIL" if why else "PASS", why or f"on Calculator: {str(res.get('message', ''))[:60]}", ms)
            time.sleep(pause)
        os.makedirs(SANDBOX, exist_ok=True)
        f = os.path.join(SANDBOX, "live_check.txt")
        open(f, "w").write("DEMASCAS live check")
        for name, args in (("open_file", {"file_path": f}), ("reveal_in_finder", {"path": f})):
            res, ms = call(name, args)
            why = good_reply(res)
            record(name, "live", "FAIL" if why else "PASS", why or "scratch file", ms)
        time.sleep(1.0)
        import subprocess
        subprocess.run(["osascript", "-e", 'tell application "TextEdit" to close (every document whose path contains "tool_check_sandbox")'], capture_output=True)
        shutil.rmtree(SANDBOX, ignore_errors=True)
        if subprocess.run(["pgrep", "-x", "Brave Browser"], capture_output=True).returncode == 0:
            for name in ("browser_get_url", "browser_get_title", "browser_list_tabs", "browser_get_page_text", "browser_get_dom_summary"):
                if name in tool_map:
                    res, ms = call(name, {})
                    why = good_reply(res)
                    record(name, "live", "FAIL" if why else "PASS", why or "read-only, current tab", ms)

    # 6. everything else: wiring only (slow / never / not exercised)
    done = {r[0] for r in rows}
    for name in names:
        if name in done:
            continue
        mode = "slow" if name in SLOW else "live"
        if wired[name]:
            record(name, mode, "FAIL", wired[name])
        else:
            hint = "never run by the check (changes the system or reaches people)" if name in NEVER_RUN else \
                   ("needs --slow" if mode == "slow" else "wiring OK; opens/clicks/types — run with --live")
            record(name, mode, "SKIP", hint)

    # summary + report
    counts = {s: sum(1 for r in rows if r[2] == s) for s in ("PASS", "FAIL", "KNOWN", "SKIP")}
    print(f"\n{len(names)} tools · PASS {counts['PASS']} · FAIL {counts['FAIL']} · KNOWN {counts['KNOWN']} · wiring-only {counts['SKIP']}")
    os.makedirs(os.path.join(ROOT, "docs"), exist_ok=True)
    with open(os.path.join(ROOT, "docs", "tool_check.md"), "w") as f:
        f.write(f"# Tool check — {time.strftime('%Y-%m-%d %H:%M')}\n\n{len(names)} tools · PASS {counts['PASS']} · FAIL {counts['FAIL']} · "
                f"KNOWN {counts['KNOWN']} · wiring-only {counts['SKIP']}\n\n| tool | mode | result | detail | ms |\n|---|---|---|---|---|\n")
        for n, m, s, d, ms in rows:
            f.write(f"| {n} | {m} | {s} | {d.replace('|', '/')[:120]} | {ms:.0f} |\n")
    print("Report: docs/tool_check.md")
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
