"""
DEMASCAS — core/screen/act.py
Act on ONE element from the last observation ("[13]"), never on pixels a
model guessed.  The tool server's gate has already asked the user when the
element is risky (core/screen/risk.py); these functions check again
(defence in depth) and then:

  1. focus guard   — the app / page is still the one that was observed
  2. hands-off     — the user isn't using the mouse or keyboard right now
  3. budget        — ≤25 actions a minute, ≤2 tries on the same element
  4. act           — page script (Brave) / accessibility (apps) / a click on
                     a recognised text box (OCR) as the last resort
  5. look again    — what changed is returned, with the fresh element list
  6. remember      — the old value / state, so "undo that" can put it back
"""

from __future__ import annotations

import json
import os
import time

from core import screen
from core.screen import risk

_STALE_SEC = 120
_HISTORY: list[tuple[float, str, int]] = []      # (when, action, element id)
_UNDO: list[dict] = []
_AUDIT = os.path.join(os.path.expanduser("~"), ".demascas", "audit.log")


def _ctx(obs: dict) -> dict:
    return {k: obs.get(k, "") for k in ("app", "url", "title", "window", "dialog")}


def _audit(line: str) -> None:
    try:
        os.makedirs(os.path.dirname(_AUDIT), exist_ok=True)
        with open(_AUDIT, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "  screen  " + line.replace("\n", " ")[:300] + "\n")
    except OSError:
        pass


def _user_active(within: float = 1.0) -> bool:
    """A real person moved the mouse / pressed a key in the last `within` s
    (hardware events only — DEMASCAS's own clicks don't count)."""
    try:
        import Quartz
        s = Quartz.CGEventSourceSecondsSinceLastEventType(Quartz.kCGEventSourceStateHIDSystemState, Quartz.kCGAnyInputEventType)
        return s < within
    except Exception:
        return False


def _budget(action: str, eid: int) -> str:
    now = time.time()
    _HISTORY[:] = [h for h in _HISTORY if now - h[0] < 60]
    if len(_HISTORY) >= 25:
        return "[-] I've done 25 screen actions in a minute — stopping here so nothing runs away. Tell me to carry on."
    if sum(1 for h in _HISTORY if h[1] == action and h[2] == eid and now - h[0] < 30) >= 2:
        return f"[-] I've already tried that twice on [{eid}] and it didn't take. I'll stop rather than keep clicking."
    _HISTORY.append((now, action, eid))
    return ""


def _front_matches(obs: dict) -> str:
    """'' if the observed app/page is still in front, else why not."""
    now_app = screen._front_app()
    if obs.get("app") and now_app and now_app != obs["app"] and now_app not in ("Terminal", "iTerm2"):
        return f"You've moved to {now_app} — should I carry on in {obs['app']}?"
    if obs.get("source") == "dom":
        from actuators.browser import browser_execute_js
        r = browser_execute_js("location.href")
        if r.get("success") and obs.get("url") and r.get("value") and r["value"] != obs["url"]:
            return "The page changed since I looked — I'll look again first."
    return ""


def _prepare(eid, action: str) -> tuple[dict | None, dict | None, str]:
    """(observation, element, error)"""
    try:
        eid = int(eid)
    except (TypeError, ValueError):
        return None, None, "[-] Give the element's number from screen_observe, like 13."
    obs = screen.last()
    if not obs or time.time() - obs.get("at", 0) > _STALE_SEC:
        return None, None, "[-] I need a fresh look first — call screen_observe, then use its numbers."
    el = next((e for e in obs.get("elements", []) if e.get("id") == eid), None)
    if el is None:
        return None, None, f"[-] There's no [{eid}] on the screen I last saw — call screen_observe again."
    why = risk.blocked_reason(_ctx(obs), el, action)
    if why:
        return None, None, "[-] " + why
    moved = _front_matches(obs)
    if moved:
        return None, None, "[-] " + moved
    if _user_active():
        return None, None, "[-] You're using the mouse or keyboard — I've paused. Say 'carry on' when you're done."
    over = _budget(action, eid)
    if over:
        return None, None, over
    return obs, el, ""


def _after(obs_before: dict, el: dict, what: str) -> dict:
    """Look again and say what changed (the model reads this next)."""
    # let it react: pages update within a frame or two, native apps animate
    time.sleep(0.15 if obs_before.get("source") == "dom" else 0.35)
    new = screen.observe(obs_before.get("app", ""))
    changes = []
    if new.get("dialog") and new.get("dialog") != obs_before.get("dialog"):
        changes.append(f"a box popped up: \"{new['dialog'][:120]}\"")
    if new.get("url") and new.get("url") != obs_before.get("url"):
        changes.append(f"the page is now {new.get('title') or new['url']}")
    elif (new.get("title") or new.get("window")) != (obs_before.get("title") or obs_before.get("window")):
        changes.append(f"now showing {new.get('title') or new.get('window')}")
    same = next((e for e in new.get("elements", []) if e.get("name") == el.get("name") and e.get("role") == el.get("role")), None)
    if same is not None:
        if "checked" in same and same.get("checked") != el.get("checked"):
            changes.append(f"\"{el['name']}\" is now {'checked' if same['checked'] else 'unchecked'}")
        if "value" in same and same.get("value") != el.get("value"):
            changes.append(f"\"{el['name']}\" now reads \"{same['value'][:60]}\"")
    if not changes and len(new.get("elements", [])) == len(obs_before.get("elements", [])):
        changes.append("nothing visible changed")
    msg = f"{what}. " + ("; ".join(changes) + "." if changes else "")
    _audit(f"{what} in {obs_before.get('app')} {risk.place_of(_ctx(obs_before))}")
    return {"success": "nothing visible changed" not in msg or what.startswith("Typed"), "changed": changes,
            "message": msg,
            "screen": ("[A DIALOG OPENED — do nothing else; tell the user what it says and ask what to do.]\n" if new.get("dialog") and new.get("dialog") != obs_before.get("dialog") else "")
                      + screen.render(new, limit=40)}


# ── executors per source ──────────────────────────────────────────────

def _dom(js_body: str, eid: int) -> dict:
    from actuators.browser import browser_execute_js
    js = ("(function(){var el=document.querySelector('[data-dmx-id=\"%d\"]');"
          "if(!el) return JSON.stringify({ok:false,why:'gone'});"
          "el.scrollIntoView({block:'center'});" % eid) + js_body + "})()"
    r = browser_execute_js(js)
    if not r.get("success"):
        return {"ok": False, "why": r.get("message", "script failed")}
    try:
        return json.loads(r.get("value") or "{}")
    except ValueError:
        return {"ok": False, "why": "no answer"}


def _ax_ref(obs: dict, eid: int):
    return (obs.get("_refs") or {}).get(eid)


def _click_point(box: list[int]) -> bool:
    try:
        import pyautogui
        x, y = box[0] + box[2] / 2, box[1] + box[3] / 2
        pyautogui.click(x, y)
        return True
    except Exception:
        return False


# ── tools ─────────────────────────────────────────────────────────────

def ui_click(element_id: int) -> dict:
    """Click element [element_id] from the last screen_observe."""
    obs, el, err = _prepare(element_id, "click")
    if err:
        return {"success": False, "message": err}
    eid, label = el["id"], el.get("name", "")
    # clicking a box that's already ticked would UNtick it — clicks only ever tick
    if el.get("role") in ("checkbox", "radio") and el.get("checked"):
        return {"success": True, "message": f"\"{label}\" is already checked."}
    if el.get("disabled"):
        return {"success": False, "message": f"[-] \"{label}\" is greyed out — something else needs doing first."}
    src = obs.get("source")
    if src == "dom":
        r = _dom("el.click();return JSON.stringify({ok:true});", eid)
        if not r.get("ok"):
            return {"success": False, "message": f"[-] I couldn't click \"{label}\" ({r.get('why')})."}
    elif src == "ax":
        import ApplicationServices as AS
        ref = _ax_ref(obs, eid)
        if ref is None or AS.AXUIElementPerformAction(ref, "AXPress") != 0:
            if not (el.get("box") and _click_point(el["box"])):
                return {"success": False, "message": f"[-] I couldn't press \"{label}\"."}
    else:
        if not (el.get("box") and _click_point(el["box"])):
            return {"success": False, "message": f"[-] I couldn't click \"{label}\" (pyautogui missing?)."}
    return _after(obs, el, f"Clicked [{eid}] \"{label}\"")


def ui_type(element_id: int, text: str, replace: bool = True) -> dict:
    """Type `text` into field [element_id] (replacing what's there unless replace=false)."""
    obs, el, err = _prepare(element_id, "type")
    if err:
        return {"success": False, "message": err}
    if el["role"] not in ("textfield", "textarea", "select", "text"):
        return {"success": False, "message": f"[-] [{el['id']}] is a {el['role']}, not a text field."}
    eid, label, src = el["id"], el.get("name", ""), obs.get("source")
    old = el.get("value", "")
    if src == "dom":
        body = ("var t=%s, rep=%s;el.focus();var old=(el.value!==undefined&&el.tagName!=='DIV')?el.value:el.innerText;"
                "if(el.isContentEditable||el.tagName==='DIV'){if(rep){document.execCommand('selectAll',false,null);}"
                "document.execCommand('insertText',false,t);}else{"
                "var proto=el.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;"
                "var set=Object.getOwnPropertyDescriptor(proto,'value').set;set.call(el,rep?t:old+t);"
                "el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));}"
                "return JSON.stringify({ok:true,old:old});") % (json.dumps(text), "true" if replace else "false")
        r = _dom(body, eid)
        if not r.get("ok"):
            return {"success": False, "message": f"[-] I couldn't type into \"{label}\" ({r.get('why')})."}
        old = r.get("old", old)
    elif src == "ax":
        import ApplicationServices as AS
        ref = _ax_ref(obs, eid)
        done = False
        if ref is not None:
            AS.AXUIElementSetAttributeValue(ref, "AXFocused", True)
            new_val = text if replace else (old or "") + text
            done = AS.AXUIElementSetAttributeValue(ref, "AXValue", new_val) == 0
        if not done:   # Electron fields often ignore AXValue — type it like a person
            try:
                import pyautogui
                if el.get("box"):
                    _click_point(el["box"])
                if replace:
                    pyautogui.hotkey("command", "a")
                pyautogui.write(text, interval=0.01) if text.isascii() else _paste(text)
            except Exception as e:
                return {"success": False, "message": f"[-] I couldn't type into \"{label}\": {e}"}
    else:
        if not (el.get("box") and _click_point(el["box"])):
            return {"success": False, "message": f"[-] I couldn't reach \"{label}\"."}
        _paste(text)
    _UNDO.append({"kind": "type", "id": eid, "name": label, "old": old, "app": obs.get("app"), "url": obs.get("url")})
    shown = text if len(text) <= 60 else text[:57] + "…"
    return _after(obs, el, f"Typed \"{shown}\" into [{eid}] \"{label}\"")


def _paste(text: str) -> None:
    """Type non-ASCII / long text via the clipboard, then put the clipboard back."""
    import subprocess
    import pyautogui
    try:
        before = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=2).stdout
    except Exception:
        before = None
    subprocess.run(["pbcopy"], input=text, text=True, timeout=2)
    pyautogui.hotkey("command", "v")
    time.sleep(0.15)
    if before is not None:
        subprocess.run(["pbcopy"], input=before, text=True, timeout=2)


def ui_set_checked(element_id: int, checked: bool = True) -> dict:
    """Tick (checked=true) or untick a checkbox / pick a radio button [element_id]."""
    obs, el, err = _prepare(element_id, "check")
    if err:
        return {"success": False, "message": err}
    if el["role"] not in ("checkbox", "radio", "option"):
        return {"success": False, "message": f"[-] [{el['id']}] is a {el['role']}, not a checkbox."}
    eid, label = el["id"], el.get("name", "")
    want = bool(checked) if not isinstance(checked, str) else checked.lower() not in ("false", "0", "no", "off")
    if el.get("checked") == want:
        return {"success": True, "message": f"\"{label}\" is already {'checked' if want else 'unchecked'}."}
    if obs.get("source") == "dom":
        r = _dom("el.click();var c=(el.checked!==undefined&&el.tagName==='INPUT')?el.checked:el.getAttribute('aria-checked')==='true';"
                 "return JSON.stringify({ok:true,checked:c});", eid)
        if not r.get("ok"):
            return {"success": False, "message": f"[-] I couldn't tick \"{label}\" ({r.get('why')})."}
    else:
        import ApplicationServices as AS
        ref = _ax_ref(obs, eid)
        if ref is None or AS.AXUIElementPerformAction(ref, "AXPress") != 0:
            if not (el.get("box") and _click_point(el["box"])):
                return {"success": False, "message": f"[-] I couldn't tick \"{label}\"."}
    _UNDO.append({"kind": "check", "id": eid, "name": label, "old": el.get("checked"), "app": obs.get("app"), "url": obs.get("url")})
    return _after(obs, el, f"{'Ticked' if want else 'Unticked'} [{eid}] \"{label}\"")


def ui_select(element_id: int, option: str) -> dict:
    """Choose `option` in dropdown [element_id]."""
    obs, el, err = _prepare(element_id, "select")
    if err:
        return {"success": False, "message": err}
    eid, label = el["id"], el.get("name", "")
    if obs.get("source") == "dom":
        body = ("var want=%s.toLowerCase();if(el.tagName==='SELECT'){var best=-1;"
                "for(var i=0;i<el.options.length;i++){var t=el.options[i].text.toLowerCase().trim();"
                "if(t===want){best=i;break;}if(best<0&&t.indexOf(want)>=0)best=i;}"
                "if(best<0)return JSON.stringify({ok:false,why:'no option like that'});"
                "var old=el.options[el.selectedIndex]?el.options[el.selectedIndex].text:'';el.selectedIndex=best;"
                "el.dispatchEvent(new Event('change',{bubbles:true}));return JSON.stringify({ok:true,old:old});}"
                "el.click();return JSON.stringify({ok:true,opened:true});") % json.dumps(option or "")
        r = _dom(body, eid)
        if not r.get("ok"):
            return {"success": False, "message": f"[-] I couldn't choose \"{option}\" in \"{label}\" ({r.get('why')})."}
        if r.get("opened"):
            res = _after(obs, el, f"Opened the \"{label}\" list")
            res["message"] = f"The list is open — now ui_click the option \"{option}\". " + res["message"]
            return res
    else:
        import ApplicationServices as AS
        ref = _ax_ref(obs, eid)
        if ref is None or AS.AXUIElementPerformAction(ref, "AXPress") != 0:
            return {"success": False, "message": f"[-] I couldn't open \"{label}\"."}
        time.sleep(0.3)
        from core import axui
        item = axui.find(obs.get("app", ""), option, roles=("AXMenuItem",))
        if not item or not axui.press(item):
            return {"success": False, "message": f"[-] I opened \"{label}\" but couldn't find \"{option}\" in it."}
    return _after(obs, el, f"Chose \"{option}\" in [{eid}] \"{label}\"")


def ui_scroll(direction: str = "down", amount: int = 5) -> dict:
    """Scroll the front window up or down (then look again)."""
    obs = screen.last()
    if obs and risk.blocked_reason(_ctx(obs)):
        return {"success": False, "message": "[-] " + risk.blocked_reason(_ctx(obs))}
    if _user_active():
        return {"success": False, "message": "[-] You're using the mouse or keyboard — I've paused."}
    try:
        import pyautogui
        pyautogui.scroll((-1 if str(direction).lower().startswith("d") else 1) * max(1, min(int(amount or 5), 20)))
    except Exception as e:
        return {"success": False, "message": f"[-] I couldn't scroll: {e}"}
    time.sleep(0.3)
    new = screen.observe(obs.get("app", "") if obs else "")
    return {"success": True, "message": f"Scrolled {direction}.", "screen": screen.render(new, limit=40)}


def ui_undo() -> dict:
    """Put back the last thing typed or ticked (only on the same page/app)."""
    if not _UNDO:
        return {"success": False, "message": "There's nothing of mine to undo."}
    u = _UNDO[-1]
    obs = screen.observe(u.get("app") or "")
    if (u.get("url") and obs.get("url") != u["url"]) or (not u.get("url") and obs.get("app") != u.get("app")):
        return {"success": False, "message": "That was on another page — I can't undo it from here."}
    el = next((e for e in obs.get("elements", []) if e.get("name") == u["name"]), None)
    if el is None:
        return {"success": False, "message": f"I can't see \"{u['name']}\" anymore, so I can't undo it."}
    if el.get("role") == "radio":
        return {"success": False, "message": f"A choice like \"{u['name']}\" can't be un-picked — tell me which option you want instead."}
    _UNDO.pop()
    if u["kind"] == "type":
        r = ui_type(el["id"], u["old"] or "", replace=True)
        _UNDO.pop() if _UNDO and _UNDO[-1].get("name") == u["name"] else None
    else:
        r = ui_set_checked(el["id"], bool(u["old"]))
        _UNDO.pop() if _UNDO and _UNDO[-1].get("name") == u["name"] else None
    r["message"] = "Undone. " + r.get("message", "")
    return r
