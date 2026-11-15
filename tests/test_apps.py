"""
Tests for app learning (core/appknowledge/learner.py) — no network, no LLM,
nothing clicked.  Run:  python -m unittest discover tests
"""

import json
import os
import sys
import tempfile
import unittest

_REAL_HOME = os.path.expanduser("~")
_HOME = tempfile.mkdtemp(prefix="demascas_apps_home_")
if os.path.isdir(os.path.join(_REAL_HOME, ".cache")):
    os.symlink(os.path.join(_REAL_HOME, ".cache"), os.path.join(_HOME, ".cache"))
os.environ["HOME"] = _HOME
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.embeddings import install as _install  # noqa: E402

_install()
from core.appknowledge import learner as L  # noqa: E402

MAC = sys.platform == "darwin"


class KeyParsing(unittest.TestCase):
    def test_formats(self):
        cases = {
            "⇧⌘M": "shift+cmd+m", "⌘ K": "cmd+k", "⌥↑": "option+up", "Cmd + K": "cmd+k",
            "Control + ALT + Arrow Key UP & Control + ALT + Arrow Key DOWN": "ctrl+option+up",
            "Shift + Esc": "shift+escape", "Shift + Page Up/Down": "shift+pageup",
        }
        for raw, want in cases.items():
            self.assertTrue(L._is_combo(raw), raw)
            self.assertEqual(L._normalize_keys(raw), want, raw)
        self.assertFalse(L._is_combo("Open settings"))
        self.assertFalse(L._is_combo("What it does"))

    @unittest.skipUnless(MAC, "Mac key choice")
    def test_picks_mac_alternative(self):
        self.assertEqual(L._normalize_keys("Ctrl/Cmd + K"), "cmd+k")
        self.assertEqual(L._normalize_keys("Ctrl + / (Windows) or Cmd + / (Mac)"), "cmd+/")
        self.assertEqual(L._normalize_keys("Ctrl + K (Cmd + K on Mac)"), "cmd+k")

    def test_rejects_non_combos(self):
        self.assertIsNone(L._pair("Edit message", "Ctrl + Up + E"))


class PageParsing(unittest.TestCase):
    def test_line_pairs(self):
        text = "Navigate Between Servers\nCmd + Option + Up\nQuick Switcher: Cmd + K\nSome prose line here."
        got = {e["task"]: e["keys"] for e in L.shortcut_pairs(text)}
        self.assertEqual(got, {"Navigate Between Servers": "cmd+option+up", "Quick Switcher": "cmd+k"})

    @unittest.skipUnless(MAC, "Mac column choice")
    def test_table_os_columns(self):
        header = ["Action", "Windows", "Mac"]
        rows = [["Show pinned messages", "Ctrl + P", "Cmd + P"], ["Upload a file", "Ctrl + Shift + U", "⇧⌘U"]]
        got = {e["task"]: e["keys"] for e in L.table_pairs(rows, header)}
        self.assertEqual(got, {"Show pinned messages": "cmd+p", "Upload a file": "shift+cmd+u"})

    @unittest.skipUnless(MAC, "Mac filter")
    def test_windows_keys_dropped_on_mac(self):
        pairs = [{"keys": "ctrl+b", "task": "Bold"}, {"keys": "cmd+b", "task": "Bold"}, {"keys": "option+up", "task": "Prev"}]
        kept = L._keep_for_this_os(pairs, "")
        self.assertEqual([e["keys"] for e in kept], ["cmd+b", "option+up"])
        self.assertEqual(len(L._keep_for_this_os(pairs, "mac")), 3)

    def test_platform_from_url_only(self):
        self.assertEqual(L._platform_page("https://keyshortcut.com/macos/discord/", ""), "mac")
        self.assertEqual(L._platform_page("https://x.com/discord-shortcuts-windows-mac-and-web/", ""), "")
        self.assertEqual(L._platform_page("https://x.com/discord-keys/", "Works on Mac too"), "")

    def test_menu_keys(self):
        self.assertEqual(L._menu_keys("N", "0"), "cmd+n")
        self.assertEqual(L._menu_keys("H", "2"), "cmd+option+h")
        self.assertEqual(L._menu_keys("\x7f", "1"), "cmd+shift+delete")
        self.assertEqual(L._menu_keys("", "0"), "")


class ManualUse(unittest.TestCase):
    def setUp(self):
        self.app = "Testcord"
        manual = {"app": self.app, "entries": [], "sources": [], "learned_at": 1}
        L._merge(manual, [
            {"task": "Show pinned messages", "how": "press_keyboard_shortcut cmd+p", "kind": "shortcut", "keys": "cmd+p"},
            {"task": "Open quick switcher", "how": "press_keyboard_shortcut cmd+k", "kind": "shortcut", "keys": "cmd+k"},
            {"task": "Zoom in", "how": "click_menu_item View > Zoom In", "kind": "menu", "menu": "View > Zoom In"},
        ])
        L.save_manual(self.app, manual)

    def test_hints_relevant(self):
        h = L.hints(self.app, "show me the pinned messages", k=1)
        self.assertIn("cmd+p", h)
        self.assertIn(self.app, L.apps_in_text("open the quick switcher in testcord"))
        self.assertEqual(L.apps_in_text("open the quick switcher"), [])

    def test_experience_verifies_and_demotes(self):
        n = L.note_outcome(self.app, [{"tool": "press_keyboard_shortcut", "args": {"keys": "cmd+k"}}], True)
        self.assertEqual(n, 1)
        L.note_outcome(self.app, [{"tool": "click_menu_item", "args": {"menu_path": "View > Zoom In"}}], False)
        by_task = {e["task"]: e for e in L.load_manual(self.app)["entries"]}
        self.assertEqual(by_task["Open quick switcher"]["verified"], 1)
        self.assertEqual(by_task["Zoom in"]["failed"], 1)
        self.assertIn("✓", L.hints(self.app, "quick switcher", k=1))

    def test_dedup(self):
        manual = L.load_manual(self.app)
        added = L._merge(manual, [{"task": "show pinned messages", "how": "x", "kind": "shortcut"}])
        self.assertEqual(added, 0)


class BuiltinSkillsFromCode(unittest.TestCase):
    def test_saved_copy_keeps_stats_not_definition(self):
        from core.skills.store import SkillStore
        d = tempfile.mkdtemp()
        stale = {"id": "learn_app", "name": "OLD", "description": "", "intents": ["old"],
                 "params": {}, "stats": {"uses": 7, "successes": 7}, "source": "builtin"}
        with open(os.path.join(d, "learn_app.json"), "w") as f:
            json.dump(stale, f)
        sk = SkillStore(directory=d).skills["learn_app"]
        self.assertEqual(sk.name, "Learn an app")          # code definition wins
        self.assertEqual(sk.stats["uses"], 7)              # learned stats survive


if __name__ == "__main__":
    unittest.main()
