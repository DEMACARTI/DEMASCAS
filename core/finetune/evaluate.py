"""
DEMASCAS — core/finetune/evaluate.py
Eval gate: replay held-out decisions through the base model and the new
adapter and compare.  An adapter is adopted only if it is MORE ACCURATE
and NOT SLOWER (latency rule).

Runs in a child process (`python -m core.finetune.evaluate ...`) so model
memory is released as soon as it finishes.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import subprocess
import sys
import time


def _action(text: str) -> dict | None:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[-1]
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start):
        depth += ch == "{"
        depth -= ch == "}"
        if depth == 0:
            try:
                obj = json.loads(text[start:i + 1])
                return obj if isinstance(obj, dict) else None
            except ValueError:
                return None
    return None


def score(pred: str, ref: str) -> dict:
    p, r = _action(pred), _action(ref)
    out = {"json": p is not None, "tool": False, "exact": False}
    if p is None or r is None:
        return out
    if "done" in r:
        out["tool"] = "done" in p
        out["exact"] = out["tool"]
        return out
    out["tool"] = p.get("tool") == r.get("tool")
    out["exact"] = out["tool"] and (p.get("args") or p.get("arguments") or {}) == \
        (r.get("args") or r.get("arguments") or {})
    return out


def _eval_one(model_id: str, adapter: str | None, rows: list[dict], max_tokens: int) -> dict:
    from mlx_lm import generate, load
    import mlx.core as mx

    model, tok = load(model_id, adapter_path=adapter) if adapter else load(model_id)
    agg = {"json": 0, "tool": 0, "exact": 0, "n": 0, "gen_tokens": 0, "gen_s": 0.0}
    for row in rows:
        msgs = row["messages"]
        prompt = tok.apply_chat_template(msgs[:-1], tokenize=False, add_generation_prompt=True)
        t0 = time.time()
        out = generate(model, tok, prompt=prompt, max_tokens=max_tokens, verbose=False)
        agg["gen_s"] += time.time() - t0
        agg["gen_tokens"] += len(tok.encode(out))
        s = score(out, msgs[-1]["content"])
        for k in ("json", "tool", "exact"):
            agg[k] += s[k]
        agg["n"] += 1
    del model
    gc.collect()
    mx.clear_cache()
    n = max(1, agg["n"])
    return {"json_valid": agg["json"] / n, "tool_acc": agg["tool"] / n,
            "exact_acc": agg["exact"] / n, "n": agg["n"],
            "tok_s": agg["gen_tokens"] / agg["gen_s"] if agg["gen_s"] else 0.0}


def decide(base: dict, tuned: dict, min_gain: float = 0.02, speed_floor: float = 0.95) -> tuple[bool, str]:
    if tuned["n"] < 5:
        return False, "too few held-out examples to judge"
    if tuned["json_valid"] < base["json_valid"]:
        return False, "adapter produced more malformed actions"
    if tuned["tool_acc"] < base["tool_acc"] + min_gain and tuned["exact_acc"] < base["exact_acc"] + min_gain:
        return False, (f"no accuracy gain (tool {base['tool_acc']:.0%}→{tuned['tool_acc']:.0%}, "
                       f"exact {base['exact_acc']:.0%}→{tuned['exact_acc']:.0%})")
    if base["tok_s"] and tuned["tok_s"] < speed_floor * base["tok_s"]:
        return False, f"slower ({base['tok_s']:.1f}→{tuned['tok_s']:.1f} tok/s)"
    return True, (f"tool {base['tool_acc']:.0%}→{tuned['tool_acc']:.0%}, "
                  f"exact {base['exact_acc']:.0%}→{tuned['exact_acc']:.0%}, "
                  f"{base['tok_s']:.1f}→{tuned['tok_s']:.1f} tok/s")


def run_subprocess(model_id: str, adapter: str, valid_path: str, limit: int = 40,
                   timeout: float = 1800, should_abort=lambda: False) -> dict:
    proc = subprocess.Popen(
        [sys.executable, "-m", "core.finetune.evaluate", "--model", model_id,
         "--adapter", adapter, "--data", valid_path, "--limit", str(limit)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    t0 = time.time()
    while proc.poll() is None:
        if should_abort() or time.time() - t0 > timeout:
            proc.kill()
            return {"aborted": True}
        time.sleep(1.0)
    lines = [ln for ln in proc.stdout.read().splitlines() if ln.startswith("{")]
    return json.loads(lines[-1]) if lines else {"error": f"exit {proc.returncode}"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--adapter", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--max-tokens", type=int, default=120)
    a = ap.parse_args()
    with open(a.data) as f:
        rows = [json.loads(ln) for ln in f if ln.strip()][: a.limit]
    base = _eval_one(a.model, None, rows, a.max_tokens)
    tuned = _eval_one(a.model, a.adapter, rows, a.max_tokens)
    adopt, reason = decide(base, tuned)
    print(json.dumps({"base": base, "adapter": tuned, "adopt": adopt, "reason": reason}))


if __name__ == "__main__":
    main()
