"""
DEMASCAS — core/llm/benchmark.py
Measure how fast the plugged-in model answers on THIS laptop, using a prompt
shaped like DEMASCAS's real tool-calling prompt (~600 tokens of rules + tool
list + context).

Reports
  cold TTFT  — time to first token, first request (prompt not cached)
  warm TTFT  — same prompt prefix again (what every later command sees)
  tokens/s   — generation speed
and a verdict against voice-assistant targets:
  fast    warm TTFT ≤ 1.0 s and ≥ 20 tok/s
  ok      warm TTFT ≤ 2.0 s and ≥ 10 tok/s
  slow    anything else → pick a smaller model
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass

import requests

from core.llm.settings import LLMConfig

_RULES = (
    "You are DEMASCAS, a voice assistant that controls this computer.\n\n"
    "RULES:\n"
    "1. To ACT (click, type, scroll, open apps, read the screen, manage files) CALL a tool. "
    "Text alone does nothing.\n"
    "2. For multi-step tasks call tools ONE AT A TIME and decide the next step after each result.\n"
    "3. Use ONLY tool names from AVAILABLE TOOLS exactly as written.\n"
    "4. If no tool can do it, reply with done JSON explaining the limitation.\n"
    "5. Questions and conversation: answer directly with {\"done\":true,\"say\":\"...\"}.\n"
    "6. Reply in 1-2 spoken sentences. No markdown.\n"
    "7. To open a website use open_url.\n"
    "8. Use exact visible labels for UI elements.\n\n"
    "OUTPUT FORMAT — exactly one JSON object per turn:\n"
    "  {\"tool\":\"tool_name\",\"args\":{\"key\":\"value\"}}\n"
    "  {\"done\":true,\"say\":\"spoken answer\"}\n\n"
    "AVAILABLE TOOLS:\n"
    "- open_application(app_name) — Launch or focus an application by name\n"
    "- open_url(url, browser) — Open a website in the browser\n"
    "- click_ui_element(element_name) — Click a visible UI element by its label\n"
    "- type_text(text, app_name, press_enter) — Type text into the focused field\n"
    "- press_keyboard_shortcut(keys) — Press a key combination like command+t\n"
    "- find_file(folder, partial_name, extension) — Search a folder for files by name\n"
    "- open_file(file_path) — Open a file with its default application\n"
    "- get_active_window_tree() — Read the names of elements on screen\n"
)
_CONTEXT = (
    "\n--- CURRENT CONTEXT ---\n"
    "Home directory: ~\nDownloads: ~/Downloads\nDocuments: ~/Documents\n"
    "Active app: Finder\nLast action: opened Downloads folder\n"
)


@dataclass
class BenchResult:
    ok: bool
    cold_ttft_s: float | None = None
    warm_ttft_s: float | None = None
    tokens_per_s: float | None = None
    tokens: int = 0
    verdict: str = "error"
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        if not self.ok:
            return f"benchmark failed: {self.error}"
        return (f"first answer (cold) {self.cold_ttft_s:.2f} s · warm {self.warm_ttft_s:.2f} s · "
                f"{self.tokens_per_s:.1f} tokens/s → {self.verdict.upper()}")


def _stream_once(cfg: LLMConfig, user: str, max_tokens: int, timeout: float):
    body = {
        "model": cfg.model,
        "messages": [{"role": "system", "content": _RULES + _CONTEXT},
                     {"role": "user", "content": user}],
        "stream": True,
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }
    t0 = time.perf_counter()
    first = None
    last = None
    chunks = 0
    with requests.post(cfg.chat_url, json=body, stream=True, timeout=(2.0, timeout)) as r:
        r.raise_for_status()
        # Read whatever bytes have arrived (read1) and split SSE lines
        # ourselves.  requests.iter_lines() waits for 512 bytes, batching
        # several tokens and distorting time-to-first-token and tokens/s;
        # byte-at-a-time readline() is accurate but slow.
        r.raw.decode_content = True
        pending = b""
        done = False

        def _lines():
            nonlocal pending, done
            while not done:
                chunk = r.raw.read1(65536) if hasattr(r.raw, "read1") else r.raw.read(1)
                if not chunk:
                    done = True
                    if pending:
                        yield pending
                    return
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    yield line

        for line in _lines():
            raw = line.decode("utf-8", "replace").strip()
            if not raw.startswith("data:"):
                continue
            data = raw[5:].strip()
            if data == "[DONE]":
                break
            try:
                j = json.loads(data)
            except ValueError:
                continue
            choices = j.get("choices") or []
            if not choices:
                continue
            token = (choices[0].get("delta") or {}).get("content") or ""
            if token:
                now = time.perf_counter()
                if first is None:
                    first = now
                last = now
                chunks += 1
    if first is None:
        raise RuntimeError("model returned no tokens")
    # Decode rate = token gaps between the first and last token (the wait
    # for [DONE] / connection close is not generation time).
    gen_time = max(last - first, 1e-6)
    return first - t0, chunks, (chunks - 1) / gen_time if chunks > 1 else 0.0


def run(cfg: LLMConfig, timeout: float = 180.0) -> BenchResult:
    if not cfg.configured:
        return BenchResult(ok=False, error="no model configured")
    try:
        cold_ttft, _, _ = _stream_once(
            cfg, "What time is it? Answer with done JSON.", 16, timeout)
        warm_ttft, n, tps = _stream_once(
            cfg, "Open my Downloads folder and tell me what you did, in one sentence.", 64, timeout)
    except Exception as e:
        return BenchResult(ok=False, error=str(e))
    if warm_ttft <= 1.0 and tps >= 20:
        verdict = "fast"
    elif warm_ttft <= 2.0 and tps >= 10:
        verdict = "ok"
    else:
        verdict = "slow"
    return BenchResult(ok=True, cold_ttft_s=round(cold_ttft, 3), warm_ttft_s=round(warm_ttft, 3),
                       tokens_per_s=round(tps, 1), tokens=n, verdict=verdict)
