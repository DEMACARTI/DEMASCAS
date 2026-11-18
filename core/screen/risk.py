"""
DEMASCAS — core/screen/risk.py
How risky is THIS click / this typing — judged by what the element is, not
just by the tool's name.  "Next" is harmless; "Place order", "Delete
account", "Submit" and "Allow" are not, even though they are all clicks.

Tiers (fail closed — unknown means ask):
  local        harmless on a page/app you've let DEMASCAS use before
  first_use    harmless-looking, but a site/app DEMASCAS hasn't acted on yet → ask once
  outward      reaches people / hands something in (send, post, submit, turn in) → always ask
  money        buy, pay, order, subscribe, transfer → always ask, never pre-approved
  destructive  delete, remove, discard, reset, sign out → always ask, never pre-approved
  security     allow, grant, install, trust, permission prompts → always ask, never pre-approved
  blocked      places DEMASCAS never acts (password fields, Keychain, banking sites,
               Privacy & Security settings, the macOS password dialog, Terminal)
"""

from __future__ import annotations

import json
import os
import re
from urllib.parse import urlparse

_HOME = os.path.join(os.path.expanduser("~"), ".demascas")
TRUST_PATH = os.path.join(_HOME, "site_trust.json")
PROTECTED_PATH = os.path.join(_HOME, "protected.json")


def _words(*pats: str) -> re.Pattern:
    return re.compile(r"\b(" + "|".join(pats) + r")\b", re.I)


MONEY = _words(r"buy( now)?", r"pay( now)?", r"place (your )?order", r"checkout", r"check out", r"purchase", r"subscribe",
               r"upgrade", r"donate", r"transfer", r"send money", r"confirm (payment|purchase|order)", r"add card",
               r"proceed to (pay|payment|checkout)", r"complete (purchase|order|payment)", r"book now", r"rent")
DESTRUCTIVE = _words(r"delete", r"remove", r"erase", r"discard", r"reset", r"wipe", r"uninstall", r"empty trash",
                     r"deactivate", r"close (my |your )?account", r"sign out", r"log ?out", r"unsubscribe", r"revoke",
                     r"move to (the )?(trash|bin)", r"trash", r"clear (all|history|data)", r"format", r"overwrite",
                     r"leave (group|server|channel)", r"block", r"report")
OUTWARD = _words(r"send", r"post", r"publish", r"share", r"submit", r"turn in", r"hand in", r"finish attempt",
                 r"submit all and finish", r"reply", r"invite", r"call", r"tweet", r"comment", r"upload", r"apply",
                 r"sign up", r"register", r"rsvp", r"accept", r"decline", r"forward")
SECURITY = _words(r"allow", r"always allow", r"grant", r"install", r"trust", r"authori[sz]e", r"approve",
                  r"enable (access|permissions?)", r"give access", r"open anyway", r"continue anyway", r"run anyway",
                  r"connect (account|wallet)", r"sign in with", r"log ?in", r"verify", r"unlock")
DIALOG_DANGER = _words(r"permanently", r"cannot be undone", r"can't be undone", r"are you sure", r"will be deleted",
                       r"payment", r"charged", r"irreversible", r"will be lost", r"without saving", r"unsaved",
                       r"discard")

NO_GO_APPS = {"Keychain Access", "1Password", "1Password 7", "Bitwarden", "LastPass", "Dashlane", "SecurityAgent",
              "Installer", "Disk Utility", "Terminal", "iTerm2", "Migration Assistant", "Boot Camp Assistant", "DEMASCAS"}
_DEFAULT_PROTECTED = [
    # banks / payments / money — DEMASCAS reads, never clicks or types here
    "paypal.com", "stripe.com", "razorpay.com", "paytm.com", "phonepe.com", "upi", "netbanking", "onlinesbi",
    "hdfcbank.com", "icicibank.com", "axisbank.com", "kotak.com", "sbi.co.in", "chase.com", "bankofamerica.com",
    "wellsfargo.com", "coinbase.com", "binance.com", "zerodha.com", "groww.in", "accounts.google.com/signin",
    "appleid.apple.com", "myaccount.google.com/security",
]


def _protected() -> list[str]:
    try:
        with open(PROTECTED_PATH) as f:
            extra = json.load(f)
        return _DEFAULT_PROTECTED + [str(x).lower() for x in extra if x]
    except (FileNotFoundError, ValueError, TypeError):
        return list(_DEFAULT_PROTECTED)


def place_of(ctx: dict) -> str:
    """The site (host) for a page, the app name otherwise — what trust is kept per."""
    url = str(ctx.get("url") or "")
    if url:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    return str(ctx.get("app") or "")


def blocked_reason(ctx: dict, element: dict | None = None, action: str = "click") -> str:
    """Why DEMASCAS must not act here at all ('' = allowed to ask/act)."""
    app = str(ctx.get("app") or "")
    if app in NO_GO_APPS:
        return f"I don't act inside {app} — that's yours to do."
    title = f"{ctx.get('window', '')} {ctx.get('title', '')}".lower()
    if app == "System Settings" and re.search(r"privacy|security|password|users|login|touch id|passkey", title):
        return "I don't change Privacy, Security or password settings."
    url = str(ctx.get("url") or "").lower()
    if url and any(p in url for p in _protected()):
        return f"{place_of(ctx)} is a banking, payment or account-security site — I'll read it, but I won't click or type there."
    if element and element.get("role") == "password":
        return "That's a password field — I never type or read passwords."
    if element and action == "type" and re.search(r"\b(otp|one[- ]time|2fa|verification code|cvv|card number|pin|ssn|aadhaar)\b",
                                                   str(element.get("name", "")), re.I):
        return "That field asks for a code or card number — please type it yourself."
    return ""


def classify(ctx: dict, element: dict, action: str = "click") -> dict:
    """{"tier", "why"} for acting on `element` in `ctx` (app/url/title/dialog)."""
    why = blocked_reason(ctx, element, action)
    if why:
        return {"tier": "blocked", "why": why}
    label = str(element.get("name") or "")
    group = str(element.get("group") or "")
    dialog = str(ctx.get("dialog") or "")
    # typing / ticking / choosing fills things in — handing in is a click on Submit
    if action in ("type", "check", "select"):
        tier = "local"
    else:
        tier = "local"
        for t, pat in (("money", MONEY), ("destructive", DESTRUCTIVE), ("security", SECURITY), ("outward", OUTWARD)):
            if pat.search(label):
                tier = t
                break
        if tier == "local" and element.get("submits"):
            tier = "outward"
        # a confirm button inside a scary dialog is as risky as the dialog
        if tier == "local" and dialog and DIALOG_DANGER.search(dialog) and re.search(r"\b(ok|yes|confirm|continue|proceed|leave|discard|don'?t save|close anyway)\b", label, re.I):
            tier = "destructive"
        if ctx.get("app") == "System Settings" and tier == "local":
            tier = "security"
    if tier == "local" and not trusted(place_of(ctx)):
        tier = "first_use"
    reasons = {
        "money": "it spends money", "destructive": "it can't easily be undone", "security": "it changes what has access",
        "outward": "it sends or hands something in", "first_use": f"I haven't acted on {place_of(ctx) or 'this'} before",
        "local": "",
    }
    return {"tier": tier, "why": reasons.get(tier, "") + (f" (in: {group})" if group and tier != "local" else "")}


NEVER_PREAPPROVED = {"money", "destructive", "security", "outward", "blocked"}


# ── per-site trust (only ever for harmless clicks) ───────────────────

def _load_trust() -> dict:
    try:
        with open(TRUST_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def trusted(place: str) -> bool:
    return bool(place) and place in _load_trust()


def trust(place: str) -> None:
    if not place:
        return
    t = _load_trust()
    import time
    t[place] = time.time()
    os.makedirs(_HOME, exist_ok=True)
    tmp = TRUST_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(t, f, indent=1)
    os.replace(tmp, TRUST_PATH)
