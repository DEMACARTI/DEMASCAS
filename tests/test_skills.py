"""
Tests for the learning layer (skills, induction, teaching, document edits).
No LLM needed.  Run:  python -m unittest discover tests
"""

import os
import sys
import tempfile
import time
import unittest

# Isolate ~/.demascas BEFORE importing anything that reads it.
_REAL_HOME = os.path.expanduser("~")
_HOME = tempfile.mkdtemp(prefix="demascas_test_home_")
# Share the embedding-model cache (~80 MB) instead of re-downloading it.
if os.path.isdir(os.path.join(_REAL_HOME, ".cache")):
    os.symlink(os.path.join(_REAL_HOME, ".cache"), os.path.join(_HOME, ".cache"))
os.environ["HOME"] = _HOME
os.environ.pop("DEMASCAS_LLM_URL", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.embeddings import install as _install_embedder  # noqa: E402

_install_embedder()

from core.skills import induce, runner  # noqa: E402
from core.skills.store import SkillStore, get_store  # noqa: E402


class SkillMatching(unittest.TestCase):
    """Built-in skills: high recall on real phrasings, zero hijacking."""

    @classmethod
    def setUpClass(cls):
        cls.store = SkillStore(directory=os.path.join(_HOME, "no_user_skills"))

    POSITIVE = [
        ("i want you to help me creating a powerpoint presentation for my upcoming project damascus",
         "create_presentation"),
        ("create a presentation about albert einstein", "create_presentation"),
        ("make me some slides on climate change", "create_presentation"),
        ("prepare a ppt on machine learning with 6 slides", "create_presentation"),
        ("change the title of slide 3 to early life", "edit_presentation"),
        ("add a slide about his nobel prize at the end", "edit_presentation"),
        ("delete slide four", "edit_presentation"),
        ("write a report on renewable energy", "create_document"),
        ("draft a letter to my landlord in word", "create_document"),
        ("make a spreadsheet to track my monthly expenses", "create_spreadsheet"),
        ("create an excel sheet for my study timetable", "create_spreadsheet"),
    ]
    NEGATIVE = [
        "open powerpoint", "close the presentation", "what time is it",
        "open my downloads folder", "who is albert einstein", "search youtube for lofi beats",
        "write an email to aman about the meeting", "remember my brother's name is neon",
        "open the slides i made yesterday", "read my latest email", "take a screenshot",
        "summarize my latest pdf", "play some music", "what's the weather in delhi",
        "write an email with my report attached", "open the presentation", "show me the deck",
        "make a new folder",
    ]

    def test_positives(self):
        for cmd, want in self.POSITIVE:
            sk, score, _ = self.store.match(cmd)
            self.assertIsNotNone(sk, f"no skill for {cmd!r} (best {score:.2f})")
            self.assertEqual(sk.id, want, cmd)

    def test_negatives(self):
        for cmd in self.NEGATIVE:
            sk, score, _ = self.store.match(cmd)
            self.assertIsNone(sk, f"{cmd!r} hijacked by {sk and sk.id} ({score:.2f})")

    def test_match_latency(self):
        self.store.match("warm the index")
        t0 = time.perf_counter()
        for _ in range(20):
            self.store.match("make a presentation about the solar system")
        per_ms = (time.perf_counter() - t0) / 20 * 1000
        self.assertLess(per_ms, 20, f"skill match took {per_ms:.1f} ms")


class Induction(unittest.TestCase):
    TRAJ = {
        "success": True,
        "command": "search youtube for lofi beats and play the first video",
        "tool_calls": [
            {"tool": "web_youtube_search", "args": {"query": "lofi beats"}, "ok": True},
            {"tool": "web_youtube_play_first", "args": {}, "ok": True},
        ],
    }
    TOOLS = {"web_youtube_search", "web_youtube_play_first"}

    def test_generalize_is_deterministic(self):
        params, steps, template = induce._generalize(self.TRAJ["command"], self.TRAJ["tool_calls"])
        self.assertEqual(list(params), ["query"])
        self.assertEqual(steps[0], {"tool": "web_youtube_search", "args": {"query": "{query}"}})
        self.assertEqual(template, "search youtube for {query} and play the first video")

    def test_learn_reuse_promote(self):
        # Two-sightings rule: the first success alone teaches nothing…
        first = dict(self.TRAJ, command="search youtube for cat videos and play the first video",
                     tool_calls=[{"tool": "web_youtube_search", "args": {"query": "cat videos"}, "ok": True},
                                 {"tool": "web_youtube_play_first", "args": {}, "ok": True}])
        self.assertIsNone(induce.induce_from_trajectory(first, self.TOOLS, paraphrase=False))
        # …the same procedure succeeding for a different request does.
        sk = induce.induce_from_trajectory(self.TRAJ, self.TOOLS, paraphrase=False)
        self.assertIsNotNone(sk)
        self.assertEqual(sk.status, "candidate")
        store = get_store()
        hit, _, _ = store.match("search youtube for jazz piano and play the first video")
        self.assertEqual(hit and hit.id, sk.id)
        for miss in ["search google for lofi beats", "open youtube", "search youtube for cats"]:
            self.assertIsNone(store.match(miss)[0], miss)
        # Parameter recovered without an LLM.
        params, missing = runner._fill_params(sk, "find rain sounds on youtube and play the first one")
        self.assertEqual(params.get("query"), "rain sounds")
        self.assertEqual(missing, [])
        # Runs the learned steps with substituted arguments; promoted after 2 wins.
        calls = []
        def dispatch(name, args):
            calls.append((name, args))
            return "ok", False, {}
        runner.run(sk, {"query": "jazz"}, dispatch)
        runner.run(sk, {"query": "rain"}, dispatch)
        self.assertEqual(calls[0], ("web_youtube_search", {"query": "jazz"}))
        self.assertEqual(store.skills[sk.id].status, "active")

    def test_nothing_varies_no_skill(self):
        traj = dict(self.TRAJ, command="play something nice")
        self.assertIsNone(induce.induce_from_trajectory(traj, self.TOOLS, paraphrase=False))

    def test_unsafe_runs_never_become_skills(self):
        typing = {"success": True, "command": "send a high message to Hatem on Discord",
                  "tool_calls": [{"tool": "open_application", "args": {"app_name": "Discord"}, "ok": True},
                                 {"tool": "type_text", "args": {"text": "hi"}, "ok": True}]}
        flailing = {"success": True, "command": "open discord and remember the spelling",
                    "tool_calls": [{"tool": "open_application", "args": {"app_name": "Discord"}, "ok": True},
                                   {"tool": "get_active_window_tree", "args": {}, "ok": False}]}
        self.assertTrue(induce.skill_material(typing, self.TOOLS | {"open_application", "type_text"}))
        self.assertTrue(induce.skill_material(flailing, self.TOOLS | {"open_application", "get_active_window_tree"}))
        self.assertIsNone(induce.induce_from_trajectory(typing, self.TOOLS | {"open_application", "type_text"},
                                                        paraphrase=False, praised=True))

    def test_hallucinated_tool_ignored(self):
        traj = dict(self.TRAJ)
        traj["tool_calls"] = [{"tool": "made_up_tool", "args": {"query": "lofi beats"}, "ok": True}]
        self.assertIsNone(induce.induce_from_trajectory(traj, self.TOOLS, paraphrase=False))


class Teaching(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(
            induce.parse_teaching("when I say good morning, open my calendar and play lofi music"),
            ("good morning", ["open my calendar", "play lofi music"]))
        self.assertIsNone(induce.parse_teaching("good morning"))

    def test_taught_skill_matches_trigger_only(self):
        sk = induce.teach("when I say study time, open my notes then play lofi music")
        self.assertEqual([s["args"]["command"] for s in sk.steps], ["open my notes", "play lofi music"])
        self.assertEqual(get_store().match("study time")[0].id, sk.id)
        self.assertIsNone(get_store().match("what time is it")[0])


class DocumentHelpers(unittest.TestCase):
    def test_json_repair(self):
        import actuators.documents as d
        import actuators.file_system as fs
        orig = fs._llm_complete
        try:
            fs._llm_complete = lambda p, max_tokens=0: '{"bullets": ["at \\(7\\) years", "b"\']}'
            self.assertEqual(d._llm_json("x", 1), {"bullets": ["at (7) years", "b"]})
        finally:
            fs._llm_complete = orig

    def test_edit_clauses_without_llm(self):
        from pptx import Presentation
        import actuators.documents as d
        prs = Presentation()
        for i in range(4):
            s = prs.slides.add_slide(prs.slide_layouts[1])
            s.shapes.title.text = f"Slide {i + 1}"
        self.assertEqual(d._clause_ops("change the title of slide 3 to Early Life", prs),
                         [{"op": "set_title", "slide": 3, "title": "Early Life"}])
        self.assertEqual(d._clause_ops("delete slide four", prs),
                         [{"op": "delete_slide", "slide": 4}])
        self.assertIsNone(d._clause_ops("make it look nicer", prs))


if __name__ == "__main__":
    unittest.main()
