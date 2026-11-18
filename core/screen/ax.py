"""
DEMASCAS — core/screen/ax.py
The front window of a native (or Electron) app as numbered elements, from
the accessibility tree: role, label, value, checked, enabled, focused and
its box on screen.  Secure (password) fields are listed but never read.
Read-only; the AXUIElement refs are kept so an action can use them.
"""

from __future__ import annotations

from core import axui

_ROLE = {
    "AXButton": "button", "AXLink": "link", "AXTab": "tab", "AXCheckBox": "checkbox", "AXRadioButton": "radio",
    "AXMenuItem": "option", "AXMenuButton": "button", "AXTextField": "textfield", "AXSearchField": "textfield",
    "AXComboBox": "select", "AXTextArea": "textarea", "AXPopUpButton": "select", "AXSecureTextField": "password",
    "AXCell": "option", "AXRow": "option", "AXSlider": "slider", "AXIncrementor": "button", "AXDisclosureTriangle": "button",
}
_CONTEXT = ("AXStaticText", "AXHeading")


def _point(v, kind: str):
    """AXValue (CGPoint / CGSize) → (a, b)."""
    try:
        import ApplicationServices as AS
        t = AS.kAXValueCGPointType if kind == "point" else AS.kAXValueCGSizeType
        ok, val = AS.AXValueGetValue(v, t, None)
        if ok:
            return (float(val.x), float(val.y)) if kind == "point" else (float(val.width), float(val.height))
    except Exception:
        pass
    return None


def _front_window(app_el):
    w = axui._attr(app_el, "AXFocusedWindow") or axui._attr(app_el, "AXMainWindow")
    if w is None:
        wins = axui._attr(app_el, "AXWindows") or []
        w = wins[0] if wins else None
    return w


def observe(app: str, max_nodes: int = 6000, max_depth: int = 40, limit: int = 150) -> dict:
    """{"success", "app", "window", "elements": [...], "texts": [...]}"""
    if not axui.available():
        return {"success": False, "message": "[-] Accessibility isn't available on this system."}
    axui.enable_electron(app)
    pid = axui.pid_of(app)
    if not pid:
        return {"success": False, "message": f"[-] {app} isn't running."}
    import ApplicationServices as AS
    app_el = AS.AXUIElementCreateApplication(pid)
    win = _front_window(app_el)
    if win is None:
        if not AS.AXIsProcessTrusted():
            return {"success": False, "message": "[-] I need Accessibility permission to see app windows "
                                                 "(System Settings → Privacy & Security → Accessibility)."}
        return {"success": False, "message": f"[-] {app} has no open window."}
    title = str(axui._attr(win, "AXTitle") or "")
    elements, texts, refs = [], [], {}
    stack, seen = [(win, 0, "")], 0
    while stack and seen < max_nodes and len(elements) < limit:
        e, d, group = stack.pop()
        seen += 1
        role_ax = str(axui._attr(e, "AXRole") or "")
        sub = str(axui._attr(e, "AXSubrole") or "")
        role = _ROLE.get(role_ax)
        if sub == "AXSecureTextField":
            role = "password"
        name = str(axui._attr(e, "AXDescription") or axui._attr(e, "AXTitle") or axui._attr(e, "AXPlaceholderValue")
                   or axui._attr(e, "AXHelp") or "").strip()
        if role:
            if name or role in ("textfield", "textarea", "password", "select"):
                o = {"id": len(elements) + 1, "role": role, "name": name[:90]}
                v = axui._attr(e, "AXValue")
                if role in ("checkbox", "radio"):
                    try:
                        o["checked"] = bool(int(v)) if v is not None else False
                    except (TypeError, ValueError):
                        o["checked"] = False
                elif role in ("textfield", "textarea", "select", "slider") and v is not None:
                    o["value"] = str(v)[:200]
                # a password field's value is never read
                if axui._attr(e, "AXEnabled") is False:
                    o["disabled"] = True
                if axui._attr(e, "AXFocused"):
                    o["focused"] = True
                pos, size = axui._attr(e, "AXPosition"), axui._attr(e, "AXSize")
                p, s = (_point(pos, "point") if pos is not None else None), (_point(size, "size") if size is not None else None)
                if p and s:
                    o["box"] = [round(p[0]), round(p[1]), round(s[0]), round(s[1])]
                if group and group != name:
                    o["group"] = group[:90]
                refs[o["id"]] = e
                elements.append(o)
        elif role_ax in _CONTEXT:
            t = str(axui._attr(e, "AXValue") or name or "").strip()
            if t and len(texts) < 60:
                texts.append(t[:160])
        if d < max_depth:
            # a group's title / heading names the controls under it (a form question)
            g = group
            if role_ax in ("AXGroup", "AXScrollArea", "AXList", "AXRadioGroup") and name:
                g = name
            kids = axui._attr(e, "AXChildren") or []
            for c in reversed(list(kids)):
                stack.append((c, d + 1, g))
    return {"success": True, "app": app, "window": title, "elements": elements, "texts": texts, "_refs": refs}
