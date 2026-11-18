"""Element-risk rules for screen actions (core/screen/risk.py)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["HOME"] = tempfile.mkdtemp(prefix="dmx-risk-")      # never the real trust file

import importlib
from core.screen import risk
importlib.reload(risk)

SHOP = {"app": "Brave Browser", "url": "https://www.amazon.in/cart", "title": "Cart"}
FORM = {"app": "Brave Browser", "url": "https://docs.google.com/forms/d/x", "title": "Quiz"}


def tier(ctx, name, action="click", **kw):
    return risk.classify(ctx, {"id": 1, "role": kw.pop("role", "button"), "name": name, **kw}, action)["tier"]


def test_money_destructive_outward_security():
    assert tier(SHOP, "Place your order") == "money"
    assert tier(SHOP, "Proceed to Buy") == "money" or tier(SHOP, "Buy Now") == "money"
    assert tier(SHOP, "Delete") == "destructive"
    assert tier(FORM, "Submit") == "outward"
    assert tier(FORM, "Next", submits=True) == "outward"
    assert tier({"app": "Brave Browser", "url": "https://x.com"}, "Allow") == "security"
    assert tier({"app": "System Settings", "window": "Displays"}, "Night Shift") == "security"


def test_harmless_click_asks_once_per_site():
    assert tier(FORM, "Next") == "first_use"
    risk.trust(risk.place_of(FORM))
    assert tier(FORM, "Next") == "local"
    # trust never covers risky buttons
    assert tier(FORM, "Submit") == "outward"


def test_blocked_places_and_fields():
    assert tier({"app": "Keychain Access"}, "Show password") == "blocked"
    assert tier({"app": "Brave Browser", "url": "https://netbanking.hdfcbank.com/x"}, "Next") == "blocked"
    assert tier(FORM, "Password", action="type", role="password") == "blocked"
    assert tier(FORM, "Enter OTP", action="type", role="textfield") == "blocked"
    assert tier({"app": "System Settings", "window": "Privacy & Security"}, "Allow") == "blocked"
    assert tier({"app": "Terminal"}, "anything", action="type", role="textfield") == "blocked"


def test_scary_dialog_confirm():
    ctx = dict(FORM, dialog="This will permanently delete 3 files. This cannot be undone.")
    risk.trust(risk.place_of(ctx))
    assert tier(ctx, "OK") == "destructive"


def test_typing_and_ticking_are_filling_in():
    risk.trust(risk.place_of(FORM))
    assert tier(FORM, "Your answer", action="type", role="textfield") == "local"
    assert tier(FORM, "I agree", action="check", role="checkbox") == "local"


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
