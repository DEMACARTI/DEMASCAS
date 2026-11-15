"""
DEMASCAS — core/finetune/train.py
LoRA fine-tuning of the plugged-in model on DEMASCAS's own verified
experience (MLX / Apple Silicon).  Runs `python -m mlx_lm lora` as a
low-priority child process that can be aborted at any moment — training
must never add latency to a command.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field


@dataclass
class TrainResult:
    ok: bool
    aborted: bool = False
    seconds: float = 0.0
    iters: int = 0
    log_tail: list = field(default_factory=list)
    error: str = ""


def iterations_for(n_train: int) -> int:
    """~2 epochs, bounded so a run stays in the tens of minutes on an M1."""
    return max(60, min(600, 2 * n_train))


def lora_command(model: str, data_dir: str, adapter_dir: str, iters: int,
                 num_layers: int = 8, max_seq: int = 2048) -> list[str]:
    return [
        sys.executable, "-m", "mlx_lm", "lora",
        "--model", model,
        "--train",
        "--data", data_dir,
        "--adapter-path", adapter_dir,
        "--fine-tune-type", "lora",
        "--mask-prompt",                 # learn the ACTION, not to echo the prompt
        "--grad-checkpoint",             # trade compute for RAM (8 GB laptops)
        "--batch-size", "1",
        "--num-layers", str(num_layers),
        "--max-seq-length", str(max_seq),
        "--iters", str(iters),
        "--learning-rate", "1e-5",
        "--steps-per-report", "10",
        "--steps-per-eval", str(max(50, iters // 4)),
        "--val-batches", "8",
        "--save-every", str(max(50, iters // 4)),
    ]


def run(model: str, data_dir: str, adapter_dir: str, iters: int,
        should_abort=lambda: False, **kw) -> TrainResult:
    cmd = lora_command(model, data_dir, adapter_dir, iters, **kw)
    t0 = time.time()
    kwargs = {}
    if os.name == "posix":
        kwargs["preexec_fn"] = lambda: os.nice(15)        # background priority
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, **kwargs)
    except OSError as e:
        return TrainResult(False, error=str(e))

    tail: list[str] = []

    def _pump():
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                tail.append(line)
                del tail[:-30]

    threading.Thread(target=_pump, daemon=True).start()
    aborted = False
    while proc.poll() is None:
        if should_abort():
            aborted = True
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            break
        time.sleep(1.0)
    ok = (not aborted and proc.returncode == 0
          and os.path.exists(os.path.join(adapter_dir, "adapters.safetensors")))
    return TrainResult(ok, aborted, time.time() - t0, iters, list(tail),
                       "" if ok or aborted else (tail[-1] if tail else f"exit {proc.returncode}"))
