"""
DEMASCAS — core/finetune/scheduler.py
Automatic fine-tuning: "learn while I'm not using the laptop".

Every few minutes (inside the tool server) check, cheapest first:
  learning.auto_finetune on · runtime is MLX · enough NEW verified examples ·
  on AC power · no DEMASCAS command for ≥ idle_minutes · enough free RAM ·
  not thermally throttled
then: build dataset → LoRA train (low priority) → eval gate → adopt.
Any command arriving aborts the run immediately (note_activity), so
training never adds latency.  The new adapter is loaded the next time
DEMASCAS starts its MLX server.
"""

from __future__ import annotations

import glob
import os
import platform
import subprocess
import threading
import time

from core.finetune import adapters, dataset, evaluate, train

_last_activity = time.time()
_abort = threading.Event()
_running = threading.Lock()
_state: dict = {"phase": "idle", "detail": "", "last_run": None}


def note_activity() -> None:
    """Called on every user command — aborts training/eval in progress."""
    global _last_activity
    _last_activity = time.time()
    if _running.locked():
        _abort.set()


def state() -> dict:
    return dict(_state)


# ── resource checks ──────────────────────────────────────────────────

def on_ac_power() -> bool:
    system = platform.system()
    try:
        if system == "Darwin":
            out = subprocess.run(["pmset", "-g", "batt"], capture_output=True,
                                 text=True, timeout=3).stdout
            return "AC Power" in out.splitlines()[0] if out else True
        if system == "Linux":
            mains = [p for p in glob.glob("/sys/class/power_supply/*")
                     if open(os.path.join(p, "type")).read().strip() == "Mains"]
            if not mains:
                return True                      # desktop: no battery
            return any(open(os.path.join(p, "online")).read().strip() == "1" for p in mains)
        import psutil
        b = psutil.sensors_battery()
        return True if b is None else bool(b.power_plugged)
    except Exception:
        return False


def free_ram_gb() -> float:
    try:
        import psutil
        return psutil.virtual_memory().available / 1e9
    except Exception:
        return 0.0


def throttled() -> bool:
    if platform.system() != "Darwin":
        return False
    try:
        out = subprocess.run(["pmset", "-g", "therm"], capture_output=True, text=True, timeout=3).stdout
        for line in out.splitlines():
            if "CPU_Speed_Limit" in line:
                return int(line.split("=")[-1].strip()) < 100
    except Exception:
        pass
    return False


def _samples_at_last_run() -> int:
    rows = adapters.list_all()
    return max((r.get("samples", 0) for r in rows), default=0)


def ready(force: bool = False) -> tuple[bool, str]:
    """(can train now, reason).  `force` skips idle/AC (manual `learn`)."""
    from core.llm import settings
    ls = adapters.learning_settings()
    if not force and not ls["auto_finetune"]:
        return False, "auto fine-tuning is off"
    cfg = settings.load()
    if cfg.runtime != "mlx":
        return False, "fine-tuning currently needs the MLX runtime (Apple Silicon)"
    _, st = dataset.collect()
    if st.samples < ls["min_samples"]:
        return False, f"{st.samples}/{ls['min_samples']} training examples so far"
    if st.samples - _samples_at_last_run() < ls["min_new_samples"]:
        return False, "not enough new examples since the last training"
    if not force:
        if time.time() - _last_activity < ls["idle_minutes"] * 60:
            return False, "you're using DEMASCAS"
        if not on_ac_power():
            return False, "on battery"
    if free_ram_gb() < ls["min_free_gb"]:
        return False, f"only {free_ram_gb():.1f} GB RAM free (need {ls['min_free_gb']} GB)"
    if throttled():
        return False, "the laptop is thermally throttled"
    return True, "ready"


# ── one learning cycle ───────────────────────────────────────────────

def run_once(force: bool = False, log=print) -> dict:
    ok, why = ready(force)
    if not ok:
        return {"ran": False, "reason": why}
    if not _running.acquire(blocking=False):
        return {"ran": False, "reason": "already training"}
    _abort.clear()
    from core.llm import settings
    cfg = settings.load()
    out_dir = adapters.new_version_dir()
    meta = {"created": time.time(), "base_model": cfg.model, "adopted": False}
    try:
        _state.update(phase="dataset", detail="")
        st = dataset.build(os.path.join(out_dir, "data"))
        meta.update(samples=st.samples, train=st.train, valid=st.valid)
        log(f"[LEARN] Fine-tuning on {st.train} examples ({st.valid} held out) → {out_dir}")

        _state.update(phase="training", detail=f"{st.train} examples")
        iters = train.iterations_for(st.train)
        tr = train.run(cfg.model, os.path.join(out_dir, "data"), out_dir, iters,
                       should_abort=_abort.is_set)
        meta.update(train_seconds=round(tr.seconds), iters=iters)
        if not tr.ok:
            meta["reason"] = "aborted (you started using DEMASCAS)" if tr.aborted else f"training failed: {tr.error}"
            log(f"[LEARN] {meta['reason']}")
            return {"ran": True, "adopted": False, "reason": meta["reason"], "path": out_dir}

        _state.update(phase="evaluating", detail="")
        ev = evaluate.run_subprocess(cfg.model, out_dir, os.path.join(out_dir, "data", "valid.jsonl"),
                                     should_abort=_abort.is_set)
        meta["metrics"] = ev
        if ev.get("aborted") or "error" in ev:
            meta["reason"] = "evaluation aborted" if ev.get("aborted") else f"evaluation failed: {ev['error']}"
        elif ev.get("adopt"):
            meta.update(adopted=True, reason=ev.get("reason", ""))
            adapters.activate(out_dir)
            log(f"[LEARN] ✓ Adopted {os.path.basename(out_dir)}: {ev.get('reason')} "
                "(loaded next time DEMASCAS starts)")
        else:
            meta["reason"] = ev.get("reason", "not better than the current model")
            log(f"[LEARN] Kept the current model: {meta['reason']}")
        return {"ran": True, "adopted": meta["adopted"], "reason": meta.get("reason", ""),
                "path": out_dir, "metrics": ev}
    finally:
        adapters.write_meta(out_dir, meta)
        _state.update(phase="idle", detail=meta.get("reason", ""), last_run=time.time())
        _running.release()


def start_background() -> threading.Thread:
    def _loop():
        while True:
            ls = adapters.learning_settings()
            time.sleep(max(60.0, ls["check_every_s"]))
            try:
                if ls["auto_finetune"]:
                    run_once()
            except Exception as e:
                print(f"[LEARN] auto fine-tune skipped: {e}")
    t = threading.Thread(target=_loop, name="auto-finetune", daemon=True)
    t.start()
    return t
