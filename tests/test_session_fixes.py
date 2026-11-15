"""
Regression tests for the problems found in the 2026-11-12 live session.
No LLM, no network, nothing clicked.  Run:  python -m unittest discover tests
"""

import os
import sys
import tempfile
import unittest

_REAL_HOME = os.path.expanduser("~")
_HOME = tempfile.mkdtemp(prefix="demascas_fix_home_")
if os.path.isdir(os.path.join(_REAL_HOME, ".cache")):
    os.symlink(os.path.join(_REAL_HOME, ".cache"), os.path.join(_HOME, ".cache"))
os.environ["HOME"] = _HOME
for k in ("DEMASCAS_LLM_URL", "DEMASCAS_LLM_MODEL"):
    os.environ.pop(k, None)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "server"))

from core.embeddings import install as _install  # noqa: E402

_install()
MAC = sys.platform == "darwin"


class Routing(unittest.TestCase):
    """#1: action requests must never be answered as small talk."""

    ACTIONS = [
        "I want you to send a message to my friend who is online called Haatem Thay and the message you should send to him is Hai",
        "send a message to Hatem Thai on Discord saying hi",
        "well you did nothing, the discord app is still open",
        "is the discord been open?", "open the friend section and discard app",
        "message aman that I'm running late", "text my mom I'll call her later",
        "what's the weather in delhi", "what's on my screen", "play some music",
    ]
    CHATS = ["how are you?", "well it was good, I watched a movie called Ramayana", "tell me a joke",
             "who made you", "I'm bored", "explain how black holes form"]

    def test_actions_never_chat(self):
        from core.routing import intent
        for c in self.ACTIONS:
            self.assertNotEqual(intent(c)[0], "chat", c)

    def test_chat_stays_chat_or_unsure(self):
        from core.routing import intent
        for c in self.CHATS:
            self.assertIn(intent(c)[0], ("chat", ""), c)


class SessionTwo(unittest.TestCase):
    """Live session 2026-11-16: garbled speech, screen questions, web curiosity."""

    def test_speech_debris_is_unclear(self):
        from core.routing import route, unclear
        self.assertEqual(route("run, run to, delete, cloud"), "unclear")
        self.assertTrue(unclear("run to, delete, cloud"))
        for real in ("open Safari, then search for cats", "apples, oranges, and bananas are fruits",
                     "send hi to Hatim on Discord"):
            self.assertFalse(unclear(real), real)

    def test_screen_questions_never_go_to_the_web(self):
        from core.web.crawler import note_question
        for q in ("what is currently being displayed?", "what is on the screen right now",
                  "which app is running", "how much battery is left"):
            self.assertFalse(note_question(q), q)


class SessionThree(unittest.TestCase):
    """Live session 2026-11-16 (second log): acting on fragments, runaway tools."""

    def test_incomplete_requests_get_a_question(self):
        from core.routing import clarify, route
        self.assertEqual(clarify("anyways, open,"), "Open what?")
        self.assertEqual(clarify("open the"), "Open what?")
        self.assertIn("button", clarify("button"))
        for fine in ("hello", "thanks", "open Discord", "what time is it"):
            self.assertEqual(clarify(fine), "", fine)
        self.assertEqual(route("anyways, open,"), "unclear")

    def test_find_file_never_scans_the_whole_disk(self):
        from actuators.file_system import find_file
        r = find_file("/", "", ".*")
        self.assertFalse(r["success"])
        self.assertEqual(r["error_type"], "too_broad")

    def test_shortcut_validation(self):
        from actuators.system_control import _build_key_command, is_risky_shortcut
        with self.assertRaises(ValueError):
            _build_key_command("cmd+shift+option+command+space")
        self.assertTrue(is_risky_shortcut("command+Q"))
        self.assertTrue(is_risky_shortcut("cmd+option+esc"))
        self.assertFalse(is_risky_shortcut("cmd+c"))

    def test_menu_path_lists_and_accelerators(self):
        from actuators.app_script import is_risky_menu
        self.assertTrue(is_risky_menu(["&File", "&Close"]))
        self.assertFalse(is_risky_menu(["View", "Zoom In"]))

    @unittest.skipUnless(MAC, "needs macOS app list")
    def test_app_names_dont_cross_wires(self):
        from core.appnames import resolve
        self.assertEqual(resolve("Dock")[0], "Dock")          # running process, not "Docker"
        self.assertNotEqual(resolve("Chrome")[0], "Home")      # 0.8 similar, different app
        self.assertEqual(resolve("Google")[0], "")


class Vocabulary(unittest.TestCase):
    """#4/#5 + the Damascus decision."""

    def setUp(self):
        from core import vocabulary
        vocabulary._cache = None
        if os.path.exists(vocabulary.VOCAB_PATH):
            os.remove(vocabulary.VOCAB_PATH)

    def test_teaching_and_correction(self):
        from core import vocabulary as v
        self.assertEqual(v.parse_teaching("open Discord, spell as DISCORD and remember it"), "Discord")
        self.assertEqual(v.parse_teaching("it's spelled D I S C O R D"), "Discord")
        self.assertIsNone(v.parse_teaching("open discord"))
        v.add_correction("this cord", "Discord")
        self.assertEqual(v.apply_corrections("open this cord for me"), "open Discord for me")
        self.assertIn("Discord", v.terms())

    def test_damascus_means_demascas_unless_city(self):
        from core.vocabulary import apply_corrections
        self.assertEqual(apply_corrections("build a presentation for Damascus"),
                         "build a presentation for DEMASCAS")
        self.assertIn("Damascus", apply_corrections("history of Damascus, Syria"))
        self.assertEqual(apply_corrections("about you, meaning the mask is, DEMAS, CAS"),
                         "about you, meaning DEMASCAS")


@unittest.skipUnless(MAC and os.path.isdir("/Applications/Discord.app"), "needs Discord installed")
class AppNames(unittest.TestCase):
    def test_misheard_names(self):
        from core.appnames import resolve
        for heard in ("cord", "this cord", "discard", "DISCORD"):
            self.assertEqual(resolve(heard)[0], "Discord", heard)
        self.assertEqual(resolve("it")[0], "")


class Messaging(unittest.TestCase):
    """#9: understand the request; never message the wrong chat."""

    def test_parse(self):
        from actuators.messaging import parse_request
        p = parse_request("send a message to Hatem Thai on Discord saying hi")
        self.assertEqual((p["app"].lower(), p["contact"], p["text"]), ("discord", "Hatem Thai", "hi"))
        p = parse_request("I want you to send a high message to Hatem Thai on Discord, which is open in my Mac")
        self.assertEqual((p["contact"], p["text"]), ("Hatem Thai", "hi"))
        p = parse_request("I want you to send a message to my friend who is online called Haatem Thay "
                          "and the message you should send to him is Hai")
        self.assertEqual((p["contact"], p["text"]), ("Haatem Thay", "Hai"))
        self.assertEqual(parse_request("message Krish on discord saying are we on tonight?")["text"],
                         "are we on tonight?")

    def test_same_person(self):
        from actuators.messaging import _same_person
        self.assertTrue(_same_person("Hatem Thai", "Hatim Tai"))
        self.assertTrue(_same_person("Krish", "Krish Mittal"))
        self.assertFalse(_same_person("Krish Mittal", "Glitchy, Krish Mittal"))   # a group
        self.assertFalse(_same_person("Hatem Thai", "Krish Mittal"))


class SkillRouting(unittest.TestCase):
    """#7: the right skill for each request from the log."""

    @classmethod
    def setUpClass(cls):
        from core.skills.store import SkillStore
        cls.store = SkillStore(directory=os.path.join(_HOME, "no_skills"))

    def _id(self, cmd):
        sk = self.store.match(cmd)[0]
        return sk.id if sk else None

    def test_routes(self):
        self.assertEqual(self._id("send a message to Hatem Thai on Discord saying hi"), "message_contact")
        self.assertEqual(self._id("can you help me use discord for studying?"), "use_app")
        self.assertEqual(self._id("learn discord"), "learn_app")
        self.assertNotEqual(self._id("can you help me use discord for studying?"), "learn_app")
        self.assertIsNone(self._id("write an email to aman about the meeting"))

    def test_spoken_readback_is_short(self):
        from core.skills.runner import say_fmt, _fmt
        long = "you, meaning DEMASCAS, and it should say what you are and what you do"
        self.assertEqual(say_fmt("about {topic}.", {"topic": long}), "about you.")
        self.assertEqual(_fmt("{q}", {"q": "jazz piano and blues"}), "jazz piano and blues")


class SelfKnowledge(unittest.TestCase):
    def test_self_topic(self):
        from core.self_profile import is_self_topic, self_facts
        for t in ("yourself", "you", "DEMASCAS", "about you"):
            self.assertTrue(is_self_topic(t), t)
        self.assertFalse(is_self_topic("black holes"))
        self.assertIn("DEMASCAS", self_facts())


class LearnCache(unittest.TestCase):
    """#6: a learned app answers instantly."""

    def test_cached(self):
        import time
        from core.appknowledge import learner as L
        m = {"app": "Discord", "entries": [{"task": "Quick switcher", "how": "cmd+k", "kind": "shortcut"}],
             "sources": [], "learned_at": time.time()}
        L.save_manual("Discord", m)
        t0 = time.time()
        r = L.learn_app("Discord")
        self.assertTrue(r.get("cached"))
        self.assertLess(time.time() - t0, 1.0)


if __name__ == "__main__":
    unittest.main()
