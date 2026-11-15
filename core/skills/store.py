"""
DEMASCAS — core/skills/store.py
Skill storage + matching.

* Built-in skills come from core/skills/builtin.py; learned/taught skills are
  JSON files in ~/.demascas/skills/ (a user file with the same id overrides a
  built-in, so stats and edits persist).
* Matching embeds the command once (cached embedder, ~1.6 ms) and compares it
  with every intent example in an in-memory matrix (~0.1 ms) — no database
  round-trip on the hot path.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

import numpy as np

from core.skills.builtin import BUILTIN_SKILLS
from core.skills.schema import Skill

SKILLS_DIR = os.path.join(os.path.expanduser("~"), ".demascas", "skills")

# Cosine-similarity thresholds (calibrated on real commands — see tests).
MATCH_THRESHOLD = 0.60      # active skills without guards
GUARDED_THRESHOLD = 0.35    # skills whose keyword guards passed
CANDIDATE_THRESHOLD = 0.80  # learned-but-unproven skills need a closer match
CANDIDATE_GUARDED = 0.45    # ...unless their action-word guards passed
MARGIN = 0.03               # best skill must beat the runner-up by this much


class SkillStore:
    def __init__(self, directory: str = SKILLS_DIR):
        self.dir = directory
        self._lock = threading.RLock()
        self.skills: dict[str, Skill] = {}
        self._matrix: np.ndarray | None = None
        self._owners: list[str] = []
        self.load()

    # ── persistence ──────────────────────────────────────────────────
    def load(self) -> None:
        with self._lock:
            self.skills = {s.id: Skill.from_dict(s.to_dict()) for s in BUILTIN_SKILLS}
            if os.path.isdir(self.dir):
                for fn in sorted(os.listdir(self.dir)):
                    if not fn.endswith(".json") or fn.startswith(("_", ".")):
                        continue            # bookkeeping files (_pending.json)
                    try:
                        with open(os.path.join(self.dir, fn)) as f:
                            sk = Skill.from_dict(json.load(f))
                        base = self.skills.get(sk.id)
                        if base is not None and base.source == "builtin":
                            # Built-ins are defined in code (updates must reach
                            # users); only what was LEARNED about them persists.
                            base.stats, base.status = sk.stats, sk.status
                            continue
                        self.skills[sk.id] = sk
                    except Exception as e:
                        print(f"[SKILLS] Skipping unreadable {fn}: {e}")
            self._matrix = None

    def save(self, skill: Skill) -> None:
        with self._lock:
            skill.updated = time.time()
            self.skills[skill.id] = skill
            os.makedirs(self.dir, exist_ok=True)
            tmp = os.path.join(self.dir, f".{skill.id}.tmp")
            with open(tmp, "w") as f:
                json.dump(skill.to_dict(), f, indent=2)
            os.replace(tmp, os.path.join(self.dir, f"{skill.id}.json"))
            self._matrix = None   # re-index on next match

    # ── matching ─────────────────────────────────────────────────────
    def _index(self) -> None:
        from core.embeddings import embed
        texts, owners = [], []
        for sk in self.skills.values():
            if sk.status == "disabled":
                continue
            for it in sk.intents:
                texts.append(it)
                owners.append(sk.id)
        self._owners = owners
        self._matrix = np.array(embed(texts), dtype=np.float32) if texts else np.zeros((0, 384), np.float32)

    def match(self, command: str) -> tuple[Skill | None, float, float]:
        """(skill or None, best score, runner-up score)."""
        from core.embeddings import embed
        with self._lock:
            if self._matrix is None:
                self._index()
            if not len(self._owners):
                return None, 0.0, 0.0
            q = np.array(embed([command])[0], dtype=np.float32)
            sims = self._matrix @ q            # vectors are L2-normalised
            best: dict[str, float] = {}
            for owner, s in zip(self._owners, sims):
                if s > best.get(owner, -1.0):
                    best[owner] = float(s)
            # Drop skills whose precision guards fail for this command.
            low = command.lower()
            for sid in list(best):
                sk = self.skills[sid]
                if any(not re.search(p, low) for p in sk.require) or \
                        any(re.search(p, low) for p in sk.exclude):
                    del best[sid]
            if not best:
                return None, 0.0, 0.0
            ranked = sorted(best.items(), key=lambda kv: -kv[1])
            top_id, top = ranked[0]
            second = ranked[1][1] if len(ranked) > 1 else 0.0
            sk = self.skills[top_id]
            if sk.min_score:
                threshold = sk.min_score
            elif sk.status == "candidate":
                threshold = CANDIDATE_GUARDED if sk.require else CANDIDATE_THRESHOLD
            else:
                threshold = GUARDED_THRESHOLD if sk.require else MATCH_THRESHOLD
            if top >= threshold and top - second >= MARGIN:
                return sk, top, second
            return None, top, second


_store: SkillStore | None = None
_store_lock = threading.Lock()


def get_store() -> SkillStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = SkillStore()
        return _store
