"""
DEMASCAS — core/llm/runtimes.py
Detect local LLM runtimes and the models they offer.

Every supported runtime speaks the OpenAI-compatible API
(/v1/models + /v1/chat/completions), which is all DEMASCAS needs to chat.
Runtime-specific endpoints are only used to identify the runtime and to read
extras (model sizes, loaded state, context length).

    Runtime     default port   identified by
    Ollama      11434          GET /api/version (+ /api/tags, /api/ps)
    LM Studio   1234           GET /api/v0/models
    llama.cpp   9931 / 8080    GET /props  (older builds used 8080)
    MLX (mlx_lm) 8081 / 8080   Python http.server + mlx-community model ids
    Jan         1337           port
    anything else              /v1/models answered → "openai-compatible"

Latency: all candidates are probed in parallel with sub-second timeouts;
a refused connection returns immediately, so detection with nothing running
takes ~a few ms and with runtimes running well under a second.
"""

from __future__ import annotations

import concurrent.futures as cf
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

import requests

CONNECT_TIMEOUT = 0.3
READ_TIMEOUT = 1.5

LABELS = {
    "ollama": "Ollama",
    "lmstudio": "LM Studio",
    "llamacpp": "llama.cpp server",
    "mlx": "MLX (mlx_lm)",
    "jan": "Jan",
    "openai": "OpenAI-compatible server",
}

# (port, hint) — hints only break ties; identity comes from fingerprints.
DEFAULT_CANDIDATES = [
    (11434, "ollama"), (1234, "lmstudio"), (9931, "llamacpp"),
    (8080, None), (8081, "mlx"), (1337, "jan"),
]


@dataclass
class ModelInfo:
    id: str
    size_gb: float | None = None     # on-disk size when the runtime reports it
    params_b: float | None = None
    vision: bool | None = None
    loaded: bool = False


@dataclass
class RuntimeInfo:
    kind: str
    base_url: str
    models: list[ModelInfo] = field(default_factory=list)
    version: str = ""
    context_tokens: int | None = None   # effective context, when knowable

    @property
    def label(self) -> str:
        return LABELS.get(self.kind, self.kind)

    @property
    def chat_url(self) -> str:
        return self.base_url.rstrip("/") + "/v1/chat/completions"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["label"] = self.label
        d["chat_url"] = self.chat_url
        return d


def _get(session: requests.Session, url: str):
    try:
        r = session.get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        if r.status_code != 200:
            return None, r
        try:
            return r.json(), r
        except ValueError:
            return None, r
    except requests.RequestException:
        return None, None


_PARAMS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[bB]\b")


def _params_from_name(name: str) -> float | None:
    m = re.search(r"[-:_ ](\d+(?:\.\d+)?)b(?:\b|[-_:])", name.lower())
    return float(m.group(1)) if m else None


def _looks_vision(name: str) -> bool:
    n = name.lower()
    return any(k in n for k in ("-vl", "vl:", "vl-", "vision", "llava", "gemma3", "minicpm-v", "moondream"))


def probe(base_url: str, hint: str | None = None) -> RuntimeInfo | None:
    """Identify the runtime at base_url and list its models (None if absent)."""
    base = base_url.rstrip("/")
    s = requests.Session()
    port = urlparse(base).port

    # ── Ollama ────────────────────────────────────────────────────
    ver, _ = _get(s, base + "/api/version")
    if isinstance(ver, dict) and "version" in ver:
        rt = RuntimeInfo("ollama", base, version=str(ver.get("version", "")),
                         context_tokens=4096)  # Ollama's default num_ctx
        tags, _ = _get(s, base + "/api/tags")
        loaded_names = set()
        ps, _ = _get(s, base + "/api/ps")
        if isinstance(ps, dict):
            for m in ps.get("models", []) or []:
                loaded_names.add(m.get("name") or m.get("model"))
                if m.get("context_length"):
                    rt.context_tokens = int(m["context_length"])
        for m in (tags or {}).get("models", []) or []:
            name = m.get("name") or m.get("model") or ""
            det = m.get("details") or {}
            fams = [str(f).lower() for f in (det.get("families") or [])]
            params = None
            pm = _PARAMS_RE.search(str(det.get("parameter_size", "")))
            if pm:
                params = float(pm.group(1))
            rt.models.append(ModelInfo(
                id=name,
                size_gb=round(m.get("size", 0) / 1024 ** 3, 2) if m.get("size") else None,
                params_b=params or _params_from_name(name),
                vision=("clip" in fams or "mllama" in fams or _looks_vision(name)),
                loaded=name in loaded_names,
            ))
        return rt

    models_json, resp = _get(s, base + "/v1/models")

    # ── llama.cpp server ─────────────────────────────────────────
    props, _ = _get(s, base + "/props")
    if isinstance(props, dict) and ("default_generation_settings" in props or "n_ctx" in props):
        gen = props.get("default_generation_settings") or {}
        n_ctx = gen.get("n_ctx") or props.get("n_ctx")
        rt = RuntimeInfo("llamacpp", base, context_tokens=int(n_ctx) if n_ctx else None,
                         version=str(props.get("build_info", "")))
        for m in (models_json or {}).get("data", []) or []:
            mid = m.get("id", "")
            rt.models.append(ModelInfo(id=mid, params_b=_params_from_name(mid),
                                       vision=_looks_vision(mid), loaded=True))
        return rt

    # ── LM Studio ────────────────────────────────────────────────
    lms, _ = _get(s, base + "/api/v0/models")
    if isinstance(lms, dict) and isinstance(lms.get("data"), list):
        rt = RuntimeInfo("lmstudio", base)
        for m in lms["data"]:
            if m.get("type") not in (None, "llm", "vlm"):
                continue  # skip embedding models
            mid = m.get("id", "")
            loaded = m.get("state") == "loaded"
            rt.models.append(ModelInfo(id=mid, params_b=_params_from_name(mid),
                                       vision=(m.get("type") == "vlm") or _looks_vision(mid),
                                       loaded=loaded))
            ctx = m.get("loaded_context_length") or (m.get("max_context_length") if loaded else None)
            if loaded and ctx:
                rt.context_tokens = int(ctx)
        return rt

    if models_json is None:
        return None  # nothing OpenAI-compatible here

    # ── MLX / Jan / generic OpenAI-compatible ─────────────────────
    ids = [m.get("id", "") for m in (models_json or {}).get("data", []) or []]
    server_hdr = (resp.headers.get("Server", "") if resp is not None else "").lower()
    if any(i.startswith("mlx-community/") for i in ids) or ("python" in server_hdr and hint == "mlx"):
        kind = "mlx"
    elif hint == "jan" or port == 1337:
        kind = "jan"
    else:
        kind = "openai"
    rt = RuntimeInfo(kind, base)
    for mid in ids:
        rt.models.append(ModelInfo(id=mid, params_b=_params_from_name(mid), vision=_looks_vision(mid)))
    return rt


def detect(extra_urls: list[str] | None = None, host: str = "127.0.0.1") -> list[RuntimeInfo]:
    """Probe all known local runtime endpoints in parallel."""
    targets: dict[str, str | None] = {}
    for port, hint in DEFAULT_CANDIDATES:
        targets[f"http://{host}:{port}"] = hint
    for u in extra_urls or []:
        if u:
            p = urlparse(u)
            if p.scheme and p.netloc:
                targets.setdefault(f"{p.scheme}://{p.netloc}", None)
    found: list[RuntimeInfo] = []
    with cf.ThreadPoolExecutor(max_workers=len(targets)) as ex:
        futs = {ex.submit(probe, url, hint): url for url, hint in targets.items()}
        for f in cf.as_completed(futs):
            try:
                rt = f.result()
            except Exception:
                rt = None
            if rt is not None:
                found.append(rt)
    order = {k: i for i, k in enumerate(["ollama", "mlx", "llamacpp", "lmstudio", "jan", "openai"])}
    found.sort(key=lambda r: (order.get(r.kind, 99), r.base_url))
    return found


def reachable(chat_url: str, timeout: float = 0.5) -> bool:
    """Cheap liveness check for a configured endpoint."""
    p = urlparse(chat_url)
    if not p.scheme or not p.netloc:
        return False
    try:
        r = requests.get(f"{p.scheme}://{p.netloc}/v1/models", timeout=(timeout, 1.5))
        return r.status_code < 500
    except requests.RequestException:
        return False
