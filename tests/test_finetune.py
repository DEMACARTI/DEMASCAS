"""
Tests for the fine-tuning pipeline that don't need a GPU run:
dataset building (reward gate), eval gate, adapter versioning/rollback,
readiness checks.  Run:  python -m unittest discover tests
"""

import json
import os
import sys
import tempfile
import time
import unittest

_REAL_HOME = os.path.expanduser("~")
_HOME = tempfile.mkdtemp(prefix="demascas_ft_home_")
if os.path.isdir(os.path.join(_REAL_HOME, ".cache")):
    os.symlink(os.path.join(_REAL_HOME, ".cache"), os.path.join(_HOME, ".cache"))
os.environ["HOME"] = _HOME
for k in ("DEMASCAS_LLM_URL", "DEMASCAS_LLM_MODEL", "DEMASCAS_LLM_RUNTIME"):
    os.environ.pop(k, None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.finetune import adapters, dataset, evaluate, scheduler, train  # noqa: E402
from core.skills import trajectories  # noqa: E402

SYSTEM = "You are DEMASCAS's system agent. Reply with ONE JSON action."


def _traj(goal, action, success=True, feedback=None):
    tid = f"t{abs(hash((goal, action, success, feedback))) % 10**9}"
    return {"id": tid, "ts": time.time(), "command": goal, "path": "agent", "success": success,
            "feedback": feedback, "tool_calls": [], "skill_id": "",
            "llm_turns": [{"messages": [{"role": "system", "content": SYSTEM},
                                        {"role": "user", "content": f"Goal: {goal}"}],
                           "output": action}]}


class Dataset(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(trajectories.TRAJ_DIR, exist_ok=True)
        rows = []
        for i in range(30):
            rows.append(_traj(f"open app number {i}",
                              json.dumps({"tool": "open_application", "args": {"app_name": f"App{i}"}})))
        rows.append(_traj("open notes", '{"tool": "open_application", "args": {"app_name": "Notes"}}',
                          feedback="positive"))
        rows.append(_traj("delete everything", '{"tool": "delete_file", "args": {}}', feedback="negative"))
        rows.append(_traj("open calendar", '{"tool": "open_application"}', success=False))
        rows.append(_traj("broken", "I think I should open the app"))           # not a JSON action
        with open(os.path.join(trajectories.TRAJ_DIR, "2026-11.jsonl"), "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def test_reward_gate(self):
        samples, st = dataset.collect()
        outs = [s["messages"][-1]["content"] for s in samples]
        self.assertFalse(any("delete_file" in o for o in outs), "rejected run leaked into training")
        self.assertFalse(any("I think" in o for o in outs), "non-JSON output leaked")
        self.assertEqual(sum("Notes" in o for o in outs), 2, "praised run should count double")
        self.assertEqual(st.samples, 32)

    def test_build_split(self):
        out = os.path.join(_HOME, "ds")
        st = dataset.build(out)
        self.assertEqual(st.train + st.valid, st.samples)
        self.assertGreaterEqual(st.valid, 1)
        with open(os.path.join(out, "train.jsonl")) as f:
            row = json.loads(f.readline())
        self.assertEqual([m["role"] for m in row["messages"]], ["system", "user", "assistant"])


class EvalGate(unittest.TestCase):
    BASE = {"json_valid": 0.9, "tool_acc": 0.6, "exact_acc": 0.4, "n": 20, "tok_s": 28.0}

    def test_better_and_fast_adopted(self):
        ok, _ = evaluate.decide(self.BASE, dict(self.BASE, tool_acc=0.75, exact_acc=0.55, tok_s=27.5))
        self.assertTrue(ok)

    def test_slower_rejected(self):
        ok, why = evaluate.decide(self.BASE, dict(self.BASE, tool_acc=0.9, tok_s=20.0))
        self.assertFalse(ok)
        self.assertIn("slower", why)

    def test_no_gain_rejected(self):
        self.assertFalse(evaluate.decide(self.BASE, dict(self.BASE))[0])

    def test_scoring(self):
        ref = '{"tool": "open_application", "args": {"app_name": "Notes"}}'
        self.assertEqual(evaluate.score(ref, ref), {"json": True, "tool": True, "exact": True})
        self.assertEqual(evaluate.score('{"tool": "open_application", "args": {"app_name": "Mail"}}', ref),
                         {"json": True, "tool": True, "exact": False})
        self.assertFalse(evaluate.score("sure, opening notes", ref)["json"])


class Adapters(unittest.TestCase):
    def test_versions_activate_rollback(self):
        v1 = adapters.new_version_dir()
        open(os.path.join(v1, "adapters.safetensors"), "w").close()
        adapters.write_meta(v1, {"adopted": True, "samples": 150, "created": time.time()})
        v2 = adapters.new_version_dir()
        open(os.path.join(v2, "adapters.safetensors"), "w").close()
        adapters.write_meta(v2, {"adopted": True, "samples": 220, "created": time.time()})
        self.assertTrue(v2.endswith("v2"))
        adapters.activate(v2)
        self.assertEqual(adapters.active_path(), v2)
        self.assertEqual(adapters.rollback(), v1)
        self.assertEqual(adapters.rollback(), "")          # → base model
        self.assertEqual(adapters.active_path(), "")


class Readiness(unittest.TestCase):
    def test_needs_mlx(self):
        ok, why = scheduler.ready(force=True)
        self.assertFalse(ok)
        self.assertIn("MLX", why)

    def test_command_aborts_training(self):
        scheduler._running.acquire()
        try:
            scheduler._abort.clear()
            scheduler.note_activity()
            self.assertTrue(scheduler._abort.is_set())
        finally:
            scheduler._running.release()

    def test_lora_command(self):
        cmd = train.lora_command("m", "d", "a", 100)
        for flag in ("--train", "--mask-prompt", "--grad-checkpoint", "--adapter-path"):
            self.assertIn(flag, cmd)
        self.assertEqual(train.iterations_for(10), 60)
        self.assertEqual(train.iterations_for(5000), 600)


if __name__ == "__main__":
    unittest.main()
