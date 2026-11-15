"""
Tests for web knowledge (core/web) — no network: pages are added directly.
Run:  python -m unittest discover tests
"""

import os
import sys
import tempfile
import time
import unittest

_REAL_HOME = os.path.expanduser("~")
_HOME = tempfile.mkdtemp(prefix="demascas_web_home_")
if os.path.isdir(os.path.join(_REAL_HOME, ".cache")):
    os.symlink(os.path.join(_REAL_HOME, ".cache"), os.path.join(_HOME, ".cache"))
os.environ["HOME"] = _HOME
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.embeddings import install as _install  # noqa: E402

_install()
from core.web import crawler, fetch, store  # noqa: E402

WEBB = ("The James Webb Space Telescope launched on 25 December 2021 on an Ariane 5 rocket.\n"
        "Webb orbits the Sun near the second Lagrange point, about 1.5 million kilometres from Earth.\n"
        "Its primary mirror is made of 18 hexagonal segments of gold-plated beryllium, 6.5 metres across.\n") * 3
PASTA = ("Cook pasta in plenty of salted boiling water and stir in the first minute.\n"
         "Taste a minute before the packet time and drain while it is still slightly firm.\n") * 3


class Store(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store._chunks = None
        store.add_page("https://example.org/webb", "James Webb Space Telescope", WEBB, topic="webb")
        store.add_page("https://example.org/pasta", "How to cook pasta", PASTA, topic="pasta")

    def test_recall_relevant_only(self):
        hits = store.recall("what is the webb telescope mirror made of")
        self.assertTrue(hits and "beryllium" in hits[0]["text"])
        self.assertTrue(all("pasta" not in h["url"] for h in hits))
        self.assertEqual(store.recall("tell me a joke about cats"), [])

    def test_context_block_has_source_and_age(self):
        block = store.context_block("how far is webb from earth")
        self.assertIn("example.org", block)
        self.assertIn("read today", block)

    def test_rescrape_replaces_and_dedups(self):
        before = store.stats()["chunks"]
        store.add_page("https://example.org/pasta", "How to cook pasta", PASTA, topic="pasta")
        self.assertEqual(store.stats()["chunks"], before)

    def test_realtime_needs_fresh_pages(self):
        old = store._chunks[0]["fetched"]
        for c in store._chunks:
            c["fetched"] = time.time() - 5 * 86400
        try:
            self.assertEqual(store.recall("latest news about the webb telescope today"), [])
            self.assertTrue(store.recall("webb telescope mirror"))
        finally:
            for c in store._chunks:
                c["fetched"] = old

    def test_forget(self):
        store.add_page("https://forget.me/x", "Temp", WEBB.replace("Webb", "Hubble"), topic="temp")
        self.assertGreater(store.forget("forget.me"), 0)
        self.assertEqual([c for c in store._chunks if "forget.me" in c["url"]], [])


class Safety(unittest.TestCase):
    def test_no_local_or_private_or_file(self):
        for u in ("http://127.0.0.1:5001/tools", "http://localhost/", "http://192.168.1.1/",
                  "http://10.0.0.5/admin", "file:///etc/passwd", "ftp://example.com/x"):
            self.assertFalse(fetch.allowed(u)[0], u)


class Curiosity(unittest.TestCase):
    def test_only_world_questions_are_queued(self):
        self.assertTrue(crawler.note_question("who discovered penicillin and when"))
        for personal in ("what's on my screen", "open discord", "what is my brother's name",
                         "send hi to Hatim", "how are you"):
            self.assertFalse(crawler.note_question(personal), personal)
        self.assertFalse(crawler.note_question("who discovered penicillin and when"))   # no duplicates

    def test_watchlist(self):
        crawler.watch_topic("AI news", every_hours=6)
        self.assertEqual(crawler._next_job(), "AI news")      # due immediately
        self.assertNotEqual(crawler._next_job(), "AI news")   # then waits 6 h
        crawler.unwatch_topic("AI news")

    def test_command_pauses_background_work(self):
        crawler._job_lock.acquire()
        try:
            crawler._abort.clear()
            crawler.note_activity()
            self.assertTrue(crawler._abort.is_set())
        finally:
            crawler._job_lock.release()


if __name__ == "__main__":
    unittest.main()
