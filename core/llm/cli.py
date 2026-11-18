"""
DEMASCAS — core/llm/cli.py
Command-line setup for the plug-in LLM.

  python main.py setup            guided: profile → detect → recommend → benchmark → save
  python main.py setup --yes      accept the recommended INSTALLED model without prompts
  python main.py setup --url http://127.0.0.1:11434 --model gemma4:e2b   (explicit)
  python main.py doctor [--bench] status of hardware, model, runtime (+ speed test)
  python main.py detect           JSON list of detected runtimes/models
  python main.py llm-check        used by scripts/run.sh (exit codes below)
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time

from core.llm import benchmark, catalog, governor, hardware, recommend, runtimes, settings

EXIT_OK, EXIT_UNREACHABLE, EXIT_NOT_CONFIGURED, EXIT_MLX_AUTOSTART = 0, 1, 2, 3


def _mlx_available() -> bool:
    return importlib.util.find_spec("mlx_lm") is not None


def _ask(prompt: str, default: str = "") -> str:
    try:
        ans = input(prompt).strip()
    except EOFError:
        return default
    return ans or default


def _print_runtimes(found: list[runtimes.RuntimeInfo]) -> None:
    if not found:
        print("  (no local LLM runtime is running)")
        return
    for rt in found:
        ctx = f", context {rt.context_tokens}" if rt.context_tokens else ""
        print(f"  • {rt.label} at {rt.base_url}{ctx} — {len(rt.models)} model(s)")
        for m in rt.models[:12]:
            tags = []
            if m.loaded:
                tags.append("loaded")
            if m.vision:
                tags.append("vision")
            if m.size_gb:
                tags.append(f"{m.size_gb:.1f} GB")
            print(f"      - {m.id}" + (f"  ({', '.join(tags)})" if tags else ""))


def _config_from_pick(pk: recommend.Pick, found: list[runtimes.RuntimeInfo]) -> settings.LLMConfig:
    rt = next((r for r in found if r.base_url == pk.base_url), None)
    base = pk.base_url or {"ollama": "http://127.0.0.1:11434", "mlx": "http://127.0.0.1:8081",
                           "lmstudio": "http://127.0.0.1:1234",
                           "llamacpp": "http://127.0.0.1:9931"}.get(pk.runtime, "")
    ctx = (rt.context_tokens if rt and rt.context_tokens
           else settings.DEFAULT_CONTEXT.get(pk.runtime, 4096))
    return settings.LLMConfig(
        runtime=pk.runtime, chat_url=base + "/v1/chat/completions", model=pk.model_id,
        context_tokens=ctx, keep_loaded_minutes=15,
        autostart=(pk.runtime == "mlx"),   # MLX has no background service
    )


def cmd_setup(args) -> int:
    print("\n═══ DEMASCAS — plug in your local LLM ═══\n")
    p = hardware.profile()
    print("This laptop:\n  " + p.summary())
    b = recommend.budget(p, args.mode)
    print(f"\nModel memory budget: {b.memory_gb} GB  ({b.reason})")

    # Explicit endpoint → just configure + verify.
    if args.url and args.model:
        rt = runtimes.probe(args.url)
        cfg = settings.LLMConfig(
            runtime=(rt.kind if rt else "openai"), chat_url=args.url, model=args.model,
            context_tokens=(rt.context_tokens if rt and rt.context_tokens else 4096))
        cfg.chat_url = settings._normalize_chat_url(cfg.chat_url)
        return _finish(cfg, p, args)

    while True:
        t0 = time.perf_counter()
        found = runtimes.detect(extra_urls=[settings.load().chat_url])
        print(f"\nDetected runtimes ({(time.perf_counter() - t0) * 1000:.0f} ms):")
        _print_runtimes(found)

        _, picks = recommend.recommend(p, found, args.mode, mlx_available=_mlx_available())
        installed = [pk for pk in picks if pk.installed]
        if installed:
            print("\nInstalled models that fit this laptop:")
            for i, pk in enumerate(installed, 1):
                print(f"  {i}. {pk.model_id}  [{pk.runtime}] — {pk.why}")
            if args.yes:
                choice = installed[0]
            else:
                sel = _ask(f"\nUse which model? [1-{len(installed)}, default 1, q to quit]: ", "1")
                if sel.lower().startswith("q"):
                    return EXIT_NOT_CONFIGURED
                try:
                    choice = installed[int(sel) - 1]
                except (ValueError, IndexError):
                    choice = installed[0]
            return _finish(_config_from_pick(choice, found), p, args)

        suggestions = [pk for pk in picks if not pk.installed]
        if not suggestions:
            print("\nNo catalog model fits this laptop's memory budget. "
                  "Free up RAM or install a smaller model manually, then re-run setup.")
            return EXIT_NOT_CONFIGURED
        if any(rt.models for rt in found) and not installed:
            print("\nThe installed models are too large for this laptop to run smoothly "
                  f"(budget {b.memory_gb} GB).")
        top = suggestions[0]
        print(f"\nRecommended for this laptop: {top.label} — {top.why}")
        print(f"Runtime: {runtimes.LABELS.get(top.runtime, top.runtime)}\n\nTo install:")
        for i, step in enumerate(top.install_steps, 1):
            print(f"  {i}. {step}")
        if len(suggestions) > 1:
            print("\nAlternatives: " + ", ".join(f"{s.label} (~{s.ram_gb} GB)" for s in suggestions[1:]))

        if top.runtime == "mlx" and not args.yes:
            ans = _ask("\nUse MLX and let DEMASCAS start it automatically (scripts/run.sh)? [Y/n]: ", "y")
            if ans.lower().startswith("y"):
                cfg = _config_from_pick(top, found)
                print("\nSaved. The model downloads the first time run.sh starts the MLX server.")
                path = settings.save(cfg)
                print(f"Config: {path}")
                return EXIT_OK
        if args.yes:
            return EXIT_NOT_CONFIGURED
        ans = _ask("\nInstall it now, then press Enter to re-scan (q to quit): ", "")
        if ans.lower().startswith("q"):
            return EXIT_NOT_CONFIGURED


MIN_CONTEXT = 4096
CONTEXT_FIX = {
    "ollama": "set OLLAMA_CONTEXT_LENGTH=8192 for the Ollama server and restart it",
    "llamacpp": "start llama-server with -c 8192",
    "lmstudio": "raise 'Context Length' to 8192 in the model's load settings",
    "mlx": "MLX uses the model's full context — no change needed",
}


def _context_warning(cfg: settings.LLMConfig) -> str:
    if cfg.context_tokens >= MIN_CONTEXT:
        return ""
    fix = CONTEXT_FIX.get(cfg.runtime, "increase the runtime's context length to 8192")
    return (f"⚠ Context window is {cfg.context_tokens} tokens; DEMASCAS's tool prompt needs at least "
            f"{MIN_CONTEXT} (long tasks get trimmed). Fix: {fix}.")


def _finish(cfg: settings.LLMConfig, p: hardware.HardwareProfile, args) -> int:
    print(f"\nTesting {cfg.model} at {cfg.base_url} ...")
    warn = _context_warning(cfg)
    if warn:
        print("  " + warn)
    if not runtimes.reachable(cfg.chat_url):
        print("  ✗ The runtime isn't reachable. Start it and run setup again.")
        return EXIT_UNREACHABLE
    gov = governor.preload(cfg)
    if gov not in ("skipped",):
        print(f"  preload: {gov}")
    res = benchmark.run(cfg)
    print("  " + res.summary())
    if res.ok and res.verdict == "slow":
        print("  This model is too slow on this laptop for snappy voice replies; "
              "a smaller model is recommended (see `python main.py setup --mode balanced`).")
        if not args.yes and not _ask("  Use it anyway? [y/N]: ", "n").lower().startswith("y"):
            return EXIT_NOT_CONFIGURED
    if not res.ok and not args.yes:
        if not _ask("  Save this model anyway? [y/N]: ", "n").lower().startswith("y"):
            return EXIT_NOT_CONFIGURED
    path = settings.save(cfg)
    print(f"\n✓ Saved: {cfg.model} via {runtimes.LABELS.get(cfg.runtime, cfg.runtime)} → {path}")
    print("  Restart DEMASCAS (scripts/run.sh) to use it.")
    return EXIT_OK


def _print_memory() -> None:
    """Swap in use + the biggest apps: on 8 GB, swap is what makes replies slow."""
    import subprocess
    try:
        out = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, timeout=3).stdout
        used = float(out.split("used = ")[1].split("M")[0]) / 1024
        ps = subprocess.run(["ps", "-axo", "rss=,comm="], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return
    apps: dict[str, float] = {}
    for line in ps.splitlines():
        rss, _, path = line.strip().partition(" ")
        if not rss.isdigit():
            continue
        name = path[:path.find(".app/")].rsplit("/", 1)[-1] if ".app/" in path else path.rsplit("/", 1)[-1]
        if path.startswith(("/System/", "/usr/libexec", "/usr/sbin")) or name in ("python", "python3", "demascasd", "WindowServer"):
            continue
        apps[name] = apps.get(name, 0) + int(rss) / 1024 / 1024
    top = ", ".join(f"{n} ({g:.1f} GB)" for n, g in sorted(apps.items(), key=lambda x: -x[1])[:3])
    hint = " ⚠ high — replies will be slow; close an app below" if used > 2 else ""
    print(f"Memory   : swap {used:.1f} GB{hint}\n           biggest apps: {top}")


def cmd_doctor(args) -> int:
    p = hardware.profile()
    cfg = settings.load()
    print("Hardware : " + p.summary())
    b = recommend.budget(p)
    print(f"Budget   : {b.memory_gb} GB for the model ({b.accel})")
    if not cfg.configured:
        print("LLM      : not configured → run: python main.py setup")
        return EXIT_NOT_CONFIGURED
    cm = catalog.find(cfg.model)
    fit = ""
    if cm:
        fit = (" ✓ fits budget" if cm.ram_gb <= b.memory_gb
               else f" ⚠ needs ~{cm.ram_gb} GB, over the {b.memory_gb} GB budget (may lag)")
    print(f"LLM      : {cfg.model} via {runtimes.LABELS.get(cfg.runtime, cfg.runtime)} "
          f"({cfg.chat_url}, context {cfg.context_tokens}, source {cfg.source}){fit}")
    up = runtimes.reachable(cfg.chat_url)
    print(f"Runtime  : {'reachable' if up else 'NOT reachable'}")
    warn = _context_warning(cfg)
    if warn:
        print("Context  : " + warn)
    if cfg.vision.model:
        print(f"Vision   : {cfg.vision.model} ({cfg.vision.chat_url})")
    try:
        from core.finetune import adapters
        print("Learning : " + adapters.status_line(cfg))
    except Exception:
        pass
    if up and args.bench:
        print("Speed    : " + benchmark.run(cfg).summary())
    _print_memory()
    if getattr(args, "permissions", False):
        from core import permissions
        print("Permissions (for the app that runs this — start DEMASCAS from the same one):")
        print(permissions.report())
    return EXIT_OK if up else EXIT_UNREACHABLE


def cmd_detect(args) -> int:
    found = runtimes.detect(extra_urls=[settings.load().chat_url])
    print(json.dumps([r.to_dict() for r in found], indent=2))
    return EXIT_OK


def cmd_llm_check(args) -> int:
    """For scripts/run.sh.  When MLX must be started, prints four lines:
    model, port, LoRA adapter (or empty), server kind ("lm" = mlx_lm text-only,
    "vlm" = mlx_vlm serving text AND vision from one copy of the weights)."""
    cfg = settings.load()
    if not cfg.configured:
        return EXIT_NOT_CONFIGURED
    if runtimes.reachable(cfg.chat_url):
        return EXIT_OK
    if cfg.runtime == "mlx" and cfg.autostart:
        from urllib.parse import urlparse
        ad = cfg.adapter_path
        ad = ad if ad and os.path.exists(os.path.join(ad, "adapters.safetensors")) else ""
        kind = "lm"
        if cfg.shared_vision:
            if ad:
                # Adapters are trained with mlx_lm; untested under mlx_vlm.
                print("[!] A learned adapter is active — serving text only (no vision).",
                      file=sys.stderr)
            elif importlib.util.find_spec("mlx_vlm") is None:
                print("[!] Vision needs mlx-vlm (pip install mlx-vlm) — serving text only.",
                      file=sys.stderr)
            else:
                kind = "vlm"
        print(cfg.model)
        print(urlparse(cfg.chat_url).port or 8081)
        print(ad)
        print(kind)
        return EXIT_MLX_AUTOSTART
    return EXIT_UNREACHABLE


def cmd_learn(args) -> int:
    from core.finetune import scheduler
    ok, why = scheduler.ready(force=True)
    if not ok:
        print(f"Not training: {why}.")
        return EXIT_OK
    print("Fine-tuning on your verified DEMASCAS experience (Ctrl+C to stop)...")
    r = scheduler.run_once(force=True)
    print(("✓ Adopted " if r.get("adopted") else "Kept the current model: ")
          + str(r.get("reason", "")))
    if r.get("adopted"):
        print("  Restart DEMASCAS (scripts/run.sh) to load it.")
    return EXIT_OK


def cmd_adapters(args) -> int:
    from core.finetune import adapters
    rows = adapters.list_all()
    print("Learning : " + adapters.status_line(settings.load()))
    if not rows:
        print("No adapters trained yet.")
    for r in rows:
        m = (r.get("metrics") or {}).get("adapter") or {}
        acc = f"tool acc {m.get('tool_acc', 0):.0%}, {m.get('tok_s', 0):.1f} tok/s" if m else ""
        print(f"{'*' if r['active'] else ' '} {r['version']:4s} "
              f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(r.get('created', 0)))}  "
              f"{r.get('samples', 0):4d} examples  {'adopted' if r.get('adopted') else 'rejected'}  "
              f"{acc}  {r.get('reason', '')}")
    return EXIT_OK


def cmd_rollback(args) -> int:
    from core.finetune import adapters
    target = adapters.rollback()
    print(f"Active: {target or 'base model'}.  Restart DEMASCAS to apply.")
    return EXIT_OK


def cmd_web(args) -> int:
    from core.web import crawler, store
    st = store.stats()
    print(f"Web knowledge : {st['chunks']} notes from {st['pages']} pages on {st['sites']} sites "
          f"(cap {st['max_chunks']})")
    s = crawler._state()
    print("Following     : " + (", ".join(f"{t} (every {int(w['every_hours'])} h)" for t, w in s["watch"].items())
                                or "nothing — say 'keep an eye on <topic>'"))
    print("To research   : " + ("; ".join(s["queue"][:5]) or "nothing queued"))
    cfg = crawler.settings()
    print(f"Background    : {'on' if cfg['auto_research'] else 'off'} (idle only, "
          f"≤ {cfg['max_pages_per_hour']} pages/hour; set web.auto_research in config.json)")
    return EXIT_OK


def cmd_skills(args) -> int:
    from core.skills.store import get_store
    if getattr(args, "prune", False):
        from core.skills.induce import prune_unsafe_learned
        gone = prune_unsafe_learned()
        print(f"Disabled {len(gone)} skill(s): {gone}" if gone else "Nothing to prune.")
    for sk in sorted(get_store().skills.values(), key=lambda s: (s.source, s.name)):
        st = sk.stats
        print(f"{sk.source:8s} {sk.status:9s} {sk.name[:50]:50s} "
              f"used {st.get('uses', 0)}x, {st.get('successes', 0)} ok, avg {sk.avg_ms / 1000:.1f} s")
    return EXIT_OK


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python main.py", description="DEMASCAS LLM setup")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="choose / install a local LLM for this laptop")
    s.add_argument("--yes", action="store_true", help="accept the recommended installed model")
    s.add_argument("--mode", choices=["balanced", "quality"], default="balanced")
    s.add_argument("--url", help="runtime base or chat URL (explicit setup)")
    s.add_argument("--model", help="model id at that runtime (explicit setup)")
    d = sub.add_parser("doctor", help="show hardware, model and runtime status")
    d.add_argument("--bench", action="store_true", help="also measure model speed")
    d.add_argument("--permissions", action="store_true", help="also check macOS permissions (prompts nothing)")
    sub.add_parser("detect", help="list detected runtimes as JSON")
    sub.add_parser("llm-check", help="(internal) used by scripts/run.sh")
    sub.add_parser("learn", help="fine-tune the model on what DEMASCAS learned, now")
    sub.add_parser("adapters", help="list learned model adapters")
    sub.add_parser("rollback", help="go back to the previous adapter (or the base model)")
    sub.add_parser("web", help="what DEMASCAS has read on the web, and what it follows")
    sk = sub.add_parser("skills", help="list learned and taught skills")
    sk.add_argument("--prune", action="store_true",
                    help="disable learned skills that replay typing/sending or settings changes")
    args = ap.parse_args(argv)
    return {"setup": cmd_setup, "doctor": cmd_doctor, "detect": cmd_detect,
            "llm-check": cmd_llm_check, "learn": cmd_learn, "adapters": cmd_adapters,
            "rollback": cmd_rollback, "skills": cmd_skills, "web": cmd_web}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
