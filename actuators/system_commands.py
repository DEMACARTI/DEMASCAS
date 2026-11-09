"""
DEMASCAS — actuators/system_commands.py
Executes safe, read-only system information commands so DEMASCAS can answer
questions about system state (running processes, disk space, network, ...).

Security model
--------------
* No shell is ever invoked (``shell=False``).  The command line is tokenised
  with ``shlex`` and each program is executed directly, so command
  substitution, redirection, globbing, ``&``, ``;`` and newlines have no
  special meaning.
* Shell metacharacters are rejected outright so the LLM gets a clear error
  instead of a confusing literal argument.
* Simple pipelines (``ps aux | grep python``) are supported by chaining
  processes directly; every stage is validated independently.
* Every program must be on an allowlist, and programs that *can* mutate
  state with certain flags (find -delete, sort -o, sysctl -w, ...) have
  per-program argument validators.
"""

import os
import shlex
import subprocess

_TIMEOUT_SEC = 10
_MAX_OUTPUT = 4000

# Characters with special meaning to a shell. "|" is handled separately as
# a pipeline separator; everything else is refused.
_FORBIDDEN_CHARS = set(";&`$<>(){}\n\r\\")


def _no_flags(*bad: str):
    """Validator factory: reject any argument equal to / starting with `bad`."""
    def check(args: list[str]) -> str | None:
        for a in args:
            for b in bad:
                if a == b or (b.startswith("-") and a.startswith(b)):
                    return f"argument '{a}' is not allowed for this command"
        return None
    return check


def _only_flags_with_prefix(*prefixes: str):
    """Validator factory: every flag argument must start with one of `prefixes`."""
    def check(args: list[str]) -> str | None:
        flags = [a for a in args if a.startswith("-")]
        if not flags:
            return "a read-only query flag is required"
        for a in flags:
            if not a.startswith(prefixes):
                return f"argument '{a}' is not a read-only query"
        return None
    return check


def _check_find(args: list[str]) -> str | None:
    banned = {"-delete", "-exec", "-execdir", "-ok", "-okdir",
              "-fprint", "-fprint0", "-fprintf", "-fls"}
    for a in args:
        if a in banned:
            return f"find action '{a}' is not allowed"
    return None


def _check_sysctl(args: list[str]) -> str | None:
    for a in args:
        if a == "-w" or "=" in a:
            return "sysctl may only read values"
    return None


def _check_date(args: list[str]) -> str | None:
    # Any non-flag, non-format argument would attempt to SET the clock.
    for a in args:
        if not a.startswith(("-", "+")):
            return "date may only display the time"
    return None


def _check_ifconfig(args: list[str]) -> str | None:
    # Allow: ifconfig, ifconfig -a, ifconfig -l, ifconfig en0
    if len(args) > 1:
        return "ifconfig may only show interface status"
    if args and args[0].startswith("-") and args[0] not in ("-a", "-l"):
        return f"argument '{args[0]}' is not allowed for ifconfig"
    return None


def _check_launchctl(args: list[str]) -> str | None:
    if not args or args[0] not in ("list", "print"):
        return "launchctl may only use 'list' or 'print'"
    return None


def _check_top(args: list[str]) -> str | None:
    # Interactive top would hang until timeout; force logging mode.
    if "-l" not in args:
        return "use 'top -l 1' (non-interactive)"
    return None


# program -> validator (None = any arguments allowed)
ALLOWED_COMMANDS = {
    "ps": None,
    "df": None,
    "uptime": None,
    "whoami": None,
    "sw_vers": None,
    "system_profiler": None,
    "which": None,
    "cat": None,
    "head": None,
    "tail": _no_flags("-f", "-F"),          # follow mode never terminates
    "wc": None,
    "grep": None,
    "ls": None,
    "echo": None,
    "cal": None,
    "top": _check_top,
    "find": _check_find,
    "sort": _no_flags("-o", "--output"),     # -o writes a file
    "sysctl": _check_sysctl,
    "date": _check_date,
    "ifconfig": _check_ifconfig,
    "launchctl": _check_launchctl,
    "networksetup": _only_flags_with_prefix("-get", "-list", "-print"),
    "pmset": _only_flags_with_prefix("-g"),
}


def _validate_stage(tokens: list[str]) -> str | None:
    """Return an error string if this pipeline stage is not allowed."""
    if not tokens:
        return "empty pipeline stage"
    prog = os.path.basename(tokens[0])
    if prog != tokens[0]:
        return "use bare command names, not paths"
    if prog not in ALLOWED_COMMANDS:
        return (f"Command '{prog}' is not in the safe allowlist. "
                "Only read-only system commands are allowed.")
    validator = ALLOWED_COMMANDS[prog]
    if validator is not None:
        problem = validator(tokens[1:])
        if problem:
            return f"{prog}: {problem}"
    return None


def _parse_pipeline(command: str) -> tuple[list[list[str]] | None, str | None]:
    """Split a command into validated pipeline stages."""
    bad = sorted(c for c in set(command) if c in _FORBIDDEN_CHARS)
    if bad:
        shown = ", ".join(repr(c) for c in bad)
        return None, f"Command contains forbidden shell characters: {shown}"
    stages: list[list[str]] = []
    for raw_stage in command.split("|"):
        try:
            tokens = shlex.split(raw_stage)
        except ValueError as e:
            return None, f"Could not parse command: {e}"
        problem = _validate_stage(tokens)
        if problem:
            return None, problem
        # No shell → expand ~ ourselves so "ls ~/Downloads" keeps working.
        tokens = [os.path.expanduser(t) if t.startswith("~") else t for t in tokens]
        stages.append(tokens)
    if len(stages) > 4:
        return None, "Pipelines are limited to 4 stages."
    return stages, None


def run_shell_command(command: str) -> str:
    """
    Run a read-only shell command to gather system information.
    Allowed programs: ps, top -l 1, df, uptime, whoami, sw_vers,
    system_profiler, networksetup -get…, pmset -g, sysctl (read), launchctl
    list, ifconfig, which, cat, head, tail, wc, sort, grep, find, ls, echo,
    date, cal.  Simple pipes (|) are supported; no other shell syntax.
    Timeout is 10 seconds.
    """
    command = (command or "").strip()
    if not command:
        return "[-] Error: Empty command."

    stages, problem = _parse_pipeline(command)
    if problem:
        return f"[-] Error: {problem}"

    procs: list[subprocess.Popen] = []
    try:
        prev_stdout = None
        for i, argv in enumerate(stages):
            last = i == len(stages) - 1
            p = subprocess.Popen(
                argv,
                stdin=prev_stdout if prev_stdout is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
            )
            if prev_stdout is not None:
                prev_stdout.close()  # allow SIGPIPE to propagate upstream
            prev_stdout = p.stdout if not last else None
            procs.append(p)

        output, error = procs[-1].communicate(timeout=_TIMEOUT_SEC)
        for p in procs[:-1]:
            p.wait(timeout=_TIMEOUT_SEC)
        returncode = procs[-1].returncode
    except subprocess.TimeoutExpired:
        for p in procs:
            p.kill()
        return f"[-] Error: Command timed out after {_TIMEOUT_SEC} seconds."
    except FileNotFoundError as e:
        return f"[-] Error: Command not found: {e.filename}"
    except Exception as e:
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass
        return f"[-] Error executing command: {e}"

    output = (output or "").strip()
    error = (error or "").strip()

    # grep exits 1 when nothing matched — that is not a failure.
    if returncode != 0 and not (returncode == 1 and stages[-1][0] == "grep" and not error):
        if error:
            return f"[-] Command failed (exit code {returncode}):\n{error[:1000]}"
        return f"[-] Command failed with exit code {returncode}"

    if not output:
        return "[+] Command succeeded with no output."

    if len(output) > _MAX_OUTPUT:
        return output[:_MAX_OUTPUT] + "\n\n... [Output truncated due to length]"
    return output


def get_system_info() -> str:
    """
    Get a quick summary of the current macOS system state
    (OS version, uptime, CPU/Memory load, active user).
    """
    info = []

    try:
        sw = subprocess.check_output(["sw_vers"], text=True, timeout=5).strip()
        lines = sw.split("\n")
        version = lines[1].split(":")[1].strip() if len(lines) > 1 else "Unknown"
        info.append(f"macOS Version: {version}")
    except Exception:
        pass

    try:
        up = subprocess.check_output(["uptime"], text=True, timeout=5).strip()
        info.append(f"Uptime details: {up}")
    except Exception:
        pass

    try:
        user = subprocess.check_output(["whoami"], text=True, timeout=5).strip()
        info.append(f"Current User: {user}")
    except Exception:
        pass

    try:
        df = subprocess.check_output(["df", "-h", "/"], text=True, timeout=5).strip().split("\n")
        if len(df) > 1:
            info.append(f"Disk Space (/): {df[1]}")
    except Exception:
        pass

    if not info:
        return "[-] Could not retrieve system information."

    return "\n".join(info)


def set_default_browser(browser_name: str) -> str:
    """
    Sets the default browser on macOS using the 'defaultbrowser' CLI.
    Common names: 'safari', 'chrome', 'brave', 'firefox', 'edge'.
    """
    browser_map = {
        "brave": "brave",
        "brave browser": "brave",
        "chrome": "chrome",
        "google chrome": "chrome",
        "safari": "safari",
        "firefox": "firefox",
        "edge": "edge",
        "microsoft edge": "edge",
    }
    b_name = (browser_name or "").lower().strip()
    cli_name = browser_map.get(b_name)
    if cli_name is None:
        return (f"[-] Unknown browser '{browser_name}'. "
                f"Supported: {', '.join(sorted(set(browser_map.values())))}.")

    try:
        result = subprocess.run(
            ["defaultbrowser", cli_name],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return f"[+] Successfully set default browser to: {browser_name} ({cli_name})"
        return f"[-] Failed to set default browser: {result.stderr.strip()}"
    except FileNotFoundError:
        return "[-] The 'defaultbrowser' CLI is not installed (brew install defaultbrowser)."
    except Exception as e:
        return f"[-] Error setting default browser: {str(e)}"
