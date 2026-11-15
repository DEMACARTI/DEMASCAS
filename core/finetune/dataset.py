"""
DEMASCAS — core/finetune/dataset.py
Experience → training data.

Every agent decision (prompt → JSON action) is logged with its trajectory
(core/skills/trajectories.py).  Decisions from runs that WORKED and that you
didn't reject become supervised examples — rejection-sampling fine-tuning,
the practical laptop form of reinforcement learning: the reward (task
verified + your feedback) decides which of the model's own behaviours it
is trained to repeat.

Output: <dir>/train.jsonl + valid.jsonl in mlx_lm's chat format
  {"messages": [{"role": "system", ...}, {"role": "user", ...},
                {"role": "assistant", "content": "<the JSON action>"}]}
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass

from core.skills import trajectories

MAX_SEQ_TOKENS = 2048
_CHARS_PER_TOKEN = 3.5


@dataclass
class DatasetStats:
    trajectories: int = 0
    usable_runs: int = 0
    samples: int = 0
    train: int = 0
    valid: int = 0
    skipped_long: int = 0
    duplicates: int = 0
    positive_feedback: int = 0
    path: str = ""


def _accepted(traj: dict) -> bool:
    """Reward gate: worked, and the user didn't say it was wrong."""
    return bool(traj.get("success")) and traj.get("feedback") != "negative"


def _valid_action(output: str) -> bool:
    try:
        obj = json.loads((output or "").strip())
    except ValueError:
        return False
    return isinstance(obj, dict) and ("tool" in obj or "done" in obj)


def collect() -> tuple[list[dict], DatasetStats]:
    st = DatasetStats()
    samples: list[dict] = []
    seen: set[str] = set()
    for traj in trajectories.load_all():
        st.trajectories += 1
        if not _accepted(traj) or not traj.get("llm_turns"):
            continue
        st.usable_runs += 1
        weight = 2 if traj.get("feedback") == "positive" else 1   # explicit praise counts double
        st.positive_feedback += traj.get("feedback") == "positive"
        for turn in traj["llm_turns"]:
            msgs = turn.get("messages") or []
            out = (turn.get("output") or "").strip()
            if not msgs or not _valid_action(out):
                continue
            conv = [{"role": m.get("role"), "content": str(m.get("content", ""))}
                    for m in msgs if m.get("role") in ("system", "user", "assistant")]
            conv.append({"role": "assistant", "content": out})
            if sum(len(m["content"]) for m in conv) / _CHARS_PER_TOKEN > MAX_SEQ_TOKENS:
                st.skipped_long += 1
                continue
            key = hashlib.sha1(json.dumps(conv[-2:], sort_keys=True).encode()).hexdigest()
            if key in seen:
                st.duplicates += 1
                continue
            seen.add(key)
            for _ in range(weight):
                samples.append({"messages": conv, "_key": key})
    st.samples = len(samples)
    return samples, st


def build(out_dir: str, valid_fraction: float = 0.1) -> DatasetStats:
    """Write train/valid splits.  The split is by content hash, so an example
    never moves between train and valid as more data arrives."""
    samples, st = collect()
    os.makedirs(out_dir, exist_ok=True)
    train, valid = [], []
    for s in samples:
        bucket = int(s["_key"][:8], 16) / 0xFFFFFFFF
        (valid if bucket < valid_fraction else train).append({"messages": s["messages"]})
    if not valid and len(train) > 1:
        valid.append(train.pop())
    for name, rows in (("train", train), ("valid", valid)):
        with open(os.path.join(out_dir, f"{name}.jsonl"), "w") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    st.train, st.valid, st.path = len(train), len(valid), out_dir
    return st
