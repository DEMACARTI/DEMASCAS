"""
DEMASCAS — actuators/everyday.py
The everyday things people ask a Mac assistant for: sound, display,
battery, disk, network, media, windows, files, time zones, units, the
dictionary, notifications.  All local — nothing here talks to the internet.

Every tool returns {"success": bool, "message": str, ...}; "message" is what
DEMASCAS says.  Anything that can lose data (empty_trash) is confirmation-
gated in server/tool_server.py (CONFIRM_REQUIRED_TOOLS).
"""

from __future__ import annotations

import datetime as dt
import os
import random
import re
import shutil
import socket
import subprocess
import time
import zipfile

_HOME = os.path.expanduser("~")


def _osa(script: str, timeout: int = 8) -> tuple[bool, str]:
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0, (r.stdout or r.stderr).strip()


def _ok(msg: str, **kw) -> dict:
    return {"success": True, "message": msg, **kw}


def _fail(msg: str) -> dict:
    return {"success": False, "message": "[-] " + msg}


def _home_path(path: str) -> str | None:
    """A path inside the user's home folder (files tools never touch the system)."""
    if not path:
        return None
    from core.utils import resolve_safe_path
    p = os.path.realpath(str(resolve_safe_path(path)))
    return p if p == _HOME or p.startswith(_HOME + os.sep) or p.startswith("/private/tmp/") or p.startswith("/tmp/") else None


# ── sound ─────────────────────────────────────────────────────────────

def get_volume() -> dict:
    """The output volume (0–100) and whether it's muted."""
    ok, out = _osa("get volume settings")
    if not ok:
        return _fail("I couldn't read the volume.")
    vol = re.search(r"output volume:(\d+)", out)
    muted = "output muted:true" in out
    v = int(vol.group(1)) if vol else 0
    return _ok(f"The volume is {v}%" + (", but it's muted." if muted else "."), volume=v, muted=muted)


def set_volume(level: str) -> dict:
    """Set the output volume: 0–100, or "up" / "down" (±10)."""
    lv = str(level).strip().lower()
    if lv in ("up", "louder", "down", "quieter", "lower"):
        cur = get_volume().get("volume", 50)
        v = max(0, min(100, cur + (10 if lv in ("up", "louder") else -10)))
    else:
        try:
            v = max(0, min(100, int(float(lv.rstrip("%")))))
        except (TypeError, ValueError):
            return _fail("Give a volume between 0 and 100, or say up or down.")
    ok, _ = _osa(f"set volume output volume {v} without output muted")
    return _ok(f"Volume set to {v}%.", volume=v) if ok else _fail("I couldn't change the volume.")


def mute(on: bool = True) -> dict:
    """Mute (on=true) or unmute the sound."""
    on = str(on).lower() not in ("false", "0", "no", "off")
    ok, _ = _osa(f"set volume output muted {'true' if on else 'false'}")
    return _ok("Muted." if on else "Sound is back on.") if ok else _fail("I couldn't change mute.")


# ── display ───────────────────────────────────────────────────────────

def brightness(direction: str = "up", steps: int = 2) -> dict:
    """Make the screen brighter or dimmer (direction up/down, 1–16 steps)."""
    code = 144 if str(direction).lower().startswith(("u", "b", "i")) else 145
    n = max(1, min(16, int(steps or 2)))
    ok, _ = _osa(f'tell application "System Events" to repeat {n} times\nkey code {code}\nend repeat')
    word = "brighter" if code == 144 else "dimmer"
    return _ok(f"Screen {word}.") if ok else _fail("I couldn't change the brightness (Accessibility needed).")


def dark_mode(on: str = "toggle") -> dict:
    """Turn dark mode on, off, or toggle it."""
    want = str(on).lower()
    val = "not dark mode" if want in ("toggle", "", "switch") else ("true" if want in ("true", "on", "1", "yes", "dark") else "false")
    ok, _ = _osa(f'tell application "System Events" to tell appearance preferences to set dark mode to {val}')
    if not ok:
        return _fail("I couldn't change the appearance.")
    _, now = _osa('tell application "System Events" to tell appearance preferences to get dark mode')
    return _ok("Dark mode is on." if now == "true" else "Light mode is on.", dark=now == "true")


def lock_screen() -> dict:
    """Lock the Mac (the password is needed to get back in)."""
    ok, _ = _osa('tell application "System Events" to keystroke "q" using {control down, command down}')
    return _ok("Locked.") if ok else _fail("I couldn't lock the screen.")


def sleep_display() -> dict:
    """Turn the display off now (the Mac keeps running)."""
    r = subprocess.run(["pmset", "displaysleepnow"], capture_output=True, timeout=5)
    return _ok("Display off.") if r.returncode == 0 else _fail("I couldn't sleep the display.")


def keep_awake(minutes: int = 60) -> dict:
    """Keep the Mac from sleeping for a while."""
    m = max(1, min(600, int(minutes or 60)))
    subprocess.Popen(["caffeinate", "-di", "-t", str(m * 60)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return _ok(f"I'll keep the Mac awake for {m} minutes.")


# ── status ────────────────────────────────────────────────────────────

def battery_status() -> dict:
    """Battery level, charging state and time left."""
    out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
    m = re.search(r"(\d+)%;\s*([^;]+);\s*([^\s]+)?", out)
    if not m:
        return _ok("This Mac has no battery — it's on power.", percent=None)
    pct, state, left = int(m.group(1)), m.group(2).strip(), (m.group(3) or "")
    msg = f"Battery is at {pct}%, {state}"
    if re.match(r"\d+:\d+", left) and left != "0:00":
        h, mi = left.split(":")
        msg += f", about {int(h)} hours {int(mi)} minutes left" if state == "discharging" else f", full in {int(h)}h {int(mi)}m"
    return _ok(msg + ".", percent=pct, state=state)


def disk_space(path: str = "/") -> dict:
    """Free and total disk space."""
    u = shutil.disk_usage(path or "/")
    gb = lambda b: b / 1e9
    return _ok(f"{gb(u.free):.0f} GB free of {gb(u.total):.0f} GB ({u.used * 100 // u.total}% used).",
               free_gb=round(gb(u.free), 1), total_gb=round(gb(u.total), 1))


def system_load() -> dict:
    """How busy the Mac is: CPU load, memory in use, swap, and the biggest apps."""
    load1 = os.getloadavg()[0]
    cores = os.cpu_count() or 1
    swap = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, timeout=5).stdout
    sw = re.search(r"used = ([\d.]+)M", swap)
    top = subprocess.run(["top", "-l", "1", "-o", "mem", "-n", "6", "-stats", "command,mem"], capture_output=True, text=True, timeout=10).stdout
    apps = [l.split()[0] for l in top.splitlines()[-6:] if l.strip() and not l.startswith("COMMAND")][:3]
    busy = "busy" if load1 > cores * 0.8 else "fairly idle" if load1 < cores * 0.3 else "moderately busy"
    msg = f"The Mac is {busy} (load {load1:.1f} on {cores} cores)"
    if sw:
        msg += f", swap {float(sw.group(1)) / 1024:.1f} GB"
    if apps:
        msg += f". Biggest: {', '.join(apps)}"
    return _ok(msg + ".", load=round(load1, 2), cores=cores)


def ip_address() -> dict:
    """This Mac's address on the local network (nothing is sent anywhere)."""
    ip = ""
    for iface in ("en0", "en1"):
        r = subprocess.run(["ipconfig", "getifaddr", iface], capture_output=True, text=True, timeout=3)
        if r.stdout.strip():
            ip = r.stdout.strip()
            break
    host = socket.gethostname()
    return _ok(f"Your local IP address is {ip}." if ip else "You're not connected to a network.", ip=ip, host=host)


def _wifi_device() -> str:
    out = subprocess.run(["networksetup", "-listallhardwareports"], capture_output=True, text=True, timeout=5).stdout
    m = re.search(r"Hardware Port: Wi-Fi\nDevice: (\w+)", out)
    return m.group(1) if m else "en0"


def wifi_status() -> dict:
    """Is Wi-Fi on, and is the Mac online?"""
    dev = _wifi_device()
    power = subprocess.run(["networksetup", "-getairportpower", dev], capture_output=True, text=True, timeout=5).stdout
    on = power.strip().endswith("On")
    ip = subprocess.run(["ipconfig", "getifaddr", dev], capture_output=True, text=True, timeout=3).stdout.strip()
    msg = ("Wi-Fi is on and connected." if ip else "Wi-Fi is on but not connected.") if on else "Wi-Fi is off."
    return _ok(msg, on=on, connected=bool(ip))


def set_wifi(on: bool = True) -> dict:
    """Turn Wi-Fi on or off."""
    on = str(on).lower() not in ("false", "0", "no", "off")
    r = subprocess.run(["networksetup", "-setairportpower", _wifi_device(), "on" if on else "off"], capture_output=True, timeout=10)
    return _ok("Wi-Fi on." if on else "Wi-Fi off.") if r.returncode == 0 else _fail("I couldn't change Wi-Fi.")


def uptime() -> dict:
    """How long since the Mac was restarted."""
    out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=3).stdout
    m = re.search(r"sec = (\d+)", out)
    if not m:
        return _fail("I couldn't read the uptime.")
    secs = int(time.time()) - int(m.group(1))
    d, h = secs // 86400, secs % 86400 // 3600
    return _ok(f"Up for {d} day{'s' if d != 1 else ''} and {h} hour{'s' if h != 1 else ''}." if d else f"Up for {h} hours and {secs % 3600 // 60} minutes.",
               seconds=secs)


# ── media ─────────────────────────────────────────────────────────────

def _player() -> str:
    for app in ("Spotify", "Music"):
        r = subprocess.run(["pgrep", "-x", app], capture_output=True, timeout=2)
        if r.returncode == 0:
            return app
    return ""


def media_control(action: str = "playpause") -> dict:
    """Play, pause, next or previous track (Spotify or Music)."""
    a = str(action).lower().replace(" ", "").replace("_", "")
    verb = {"play": "play", "pause": "pause", "playpause": "playpause", "toggle": "playpause", "resume": "play",
            "next": "next track", "skip": "next track", "previous": "previous track", "back": "previous track", "prev": "previous track"}.get(a)
    if not verb:
        return _fail("Say play, pause, next or previous.")
    app = _player()
    if not app:
        return _fail("Neither Spotify nor Music is open.")
    ok, _ = _osa(f'tell application "{app}" to {verb}')
    return _ok({"play": "Playing.", "pause": "Paused.", "playpause": "Done.", "next track": "Next track.",
                "previous track": "Previous track."}[verb], app=app) if ok else _fail(f"{app} didn't respond.")


def now_playing() -> dict:
    """What song is playing."""
    app = _player()
    if not app:
        return _ok("Nothing is playing — Spotify and Music are closed.", playing=False)
    ok, out = _osa(f'tell application "{app}"\nif player state is playing then\nreturn (name of current track) & " — " & (artist of current track)\nelse\nreturn ""\nend if\nend tell')
    if ok and out:
        return _ok(f"{out} on {app}.", playing=True, track=out)
    return _ok(f"{app} is paused.", playing=False)


# ── windows ───────────────────────────────────────────────────────────

def window_action(action: str, app_name: str = "") -> dict:
    """Do something with the front window: minimize, fullscreen, left_half,
    right_half, maximize, center."""
    from core import axui
    import ApplicationServices as AS
    app = app_name or axui.app_on_screen() or axui.frontmost_app()
    pid = axui.pid_of(app)
    if not pid:
        return _fail(f"{app or 'That app'} isn't running.")
    win = axui._attr(AS.AXUIElementCreateApplication(pid), "AXFocusedWindow")
    if win is None:
        return _fail(f"{app} has no window to change." + ("" if axui.AS_trusted() else " (Accessibility is off.)"))
    a = str(action).lower().replace(" ", "_")
    if a in ("minimize", "minimise"):
        ok = AS.AXUIElementSetAttributeValue(win, "AXMinimized", True) == 0
        return _ok(f"Minimized {app}.") if ok else _fail("I couldn't minimize it.")
    if a in ("fullscreen", "full_screen"):
        cur = bool(axui._attr(win, "AXFullScreen"))
        ok = AS.AXUIElementSetAttributeValue(win, "AXFullScreen", not cur) == 0
        return _ok(f"{app} is {'out of' if cur else 'in'} full screen.") if ok else _fail("I couldn't change full screen.")
    import Quartz
    b = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
    W, H, top = int(b.size.width), int(b.size.height), 25
    frames = {"left_half": (0, top, W // 2, H - top), "right_half": (W // 2, top, W // 2, H - top),
              "maximize": (0, top, W, H - top), "maximise": (0, top, W, H - top),
              "center": (W // 6, H // 8, W * 2 // 3, H * 3 // 4)}
    if a not in frames:
        return _fail("Say minimize, full screen, left half, right half, maximize or center.")
    x, y, w, h = frames[a]
    pos = AS.AXValueCreate(AS.kAXValueCGPointType, Quartz.CGPoint(x, y))
    size = AS.AXValueCreate(AS.kAXValueCGSizeType, Quartz.CGSize(w, h))
    moved = AS.AXUIElementSetAttributeValue(win, "AXPosition", pos) == 0
    sized = AS.AXUIElementSetAttributeValue(win, "AXSize", size) == 0
    if moved and sized:
        return _ok(f"{app}: {a.replace('_', ' ')}.")
    if moved:   # fixed-size windows (Calculator, some dialogs) can still move
        return _ok(f"Moved {app} — its window has a fixed size, so I couldn't resize it.", resized=False)
    return _fail(f"{app}'s window can't be moved or resized.")


def hide_app(app_name: str) -> dict:
    """Hide an app's windows (it keeps running)."""
    import ApplicationServices as AS
    from core import axui
    for name, pid in axui.running_apps():
        if name.lower() == (app_name or "").lower():
            # AXHidden works from any thread; NSRunningApplication.hide() is
            # refused off the main run loop (tool check, 18 Nov)
            if AS.AXUIElementSetAttributeValue(AS.AXUIElementCreateApplication(pid), "AXHidden", True) == 0:
                return _ok(f"Hid {name}.")
            ok, _ = _osa(f'tell application "System Events" to set visible of process "{name}" to false')
            return _ok(f"Hid {name}.") if ok else _fail(f"{name} wouldn't hide (Accessibility needed).")
    return _fail(f"{app_name} isn't running.")


# ── files (inside your home folder only) ─────────────────────────────

def create_folder(path: str) -> dict:
    """Make a new folder (e.g. ~/Documents/Projects)."""
    p = _home_path(path)
    if not p:
        return _fail("I only make folders inside your home folder.")
    os.makedirs(p, exist_ok=True)
    return _ok(f"Made the folder {p.replace(_HOME, '~')}.", path=p)


def _dest(src: str, dest: str) -> str | None:
    d = _home_path(dest)
    if d and os.path.isdir(d):
        d = os.path.join(d, os.path.basename(src.rstrip("/")))
    return d


def move_file(source: str, destination: str) -> dict:
    """Move a file or folder (inside your home folder). Never overwrites."""
    s, d = _home_path(source), None
    if not s or not os.path.exists(s):
        return _fail(f"I can't find {source}.")
    d = _dest(s, destination)
    if not d:
        return _fail("I only move things within your home folder.")
    if os.path.exists(d):
        return _fail(f"{os.path.basename(d)} already exists there — I won't overwrite it.")
    shutil.move(s, d)
    return _ok(f"Moved {os.path.basename(s)} to {os.path.dirname(d).replace(_HOME, '~')}.", path=d)


def copy_file(source: str, destination: str) -> dict:
    """Copy a file or folder (inside your home folder). Never overwrites."""
    s = _home_path(source)
    if not s or not os.path.exists(s):
        return _fail(f"I can't find {source}.")
    d = _dest(s, destination)
    if not d:
        return _fail("I only copy within your home folder.")
    if os.path.exists(d):
        return _fail(f"{os.path.basename(d)} already exists there — I won't overwrite it.")
    (shutil.copytree if os.path.isdir(s) else shutil.copy2)(s, d)
    return _ok(f"Copied {os.path.basename(s)} to {os.path.dirname(d).replace(_HOME, '~')}.", path=d)


def rename_file(path: str, new_name: str) -> dict:
    """Rename a file or folder (keeps it in the same place)."""
    s = _home_path(path)
    if not s or not os.path.exists(s):
        return _fail(f"I can't find {path}.")
    name = os.path.basename((new_name or "").strip())
    if not name:
        return _fail("What should the new name be?")
    if "." not in name and "." in os.path.basename(s):
        name += os.path.splitext(s)[1]          # keep the extension
    d = os.path.join(os.path.dirname(s), name)
    if os.path.exists(d):
        return _fail(f"{name} already exists there.")
    os.rename(s, d)
    return _ok(f"Renamed to {name}.", path=d)


def zip_files(paths: str, destination: str = "") -> dict:
    """Compress files/folders into a .zip (paths separated by commas)."""
    items = [p for p in (_home_path(x.strip()) for x in str(paths).split(",")) if p and os.path.exists(p)]
    if not items:
        return _fail("I couldn't find those files.")
    dest = _home_path(destination) if destination else os.path.join(os.path.dirname(items[0]), "Archive.zip")
    if not dest:
        return _fail("I only write zips inside your home folder.")
    if not dest.endswith(".zip"):
        dest += ".zip"
    if os.path.exists(dest):
        return _fail(f"{os.path.basename(dest)} already exists.")
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for it in items:
            if os.path.isdir(it):
                for root, _, files in os.walk(it):
                    for f in files:
                        full = os.path.join(root, f)
                        z.write(full, os.path.relpath(full, os.path.dirname(it)))
            else:
                z.write(it, os.path.basename(it))
    return _ok(f"Made {os.path.basename(dest)} ({os.path.getsize(dest) // 1024} KB).", path=dest)


def reveal_in_finder(path: str) -> dict:
    """Show a file in Finder."""
    p = _home_path(path) or (path if path and os.path.exists(path) else None)
    if not p or not os.path.exists(p):
        return _fail(f"I can't find {path}.")
    subprocess.run(["open", "-R", p], timeout=5)
    return _ok(f"Showing {os.path.basename(p)} in Finder.")


def file_info(path: str) -> dict:
    """Size, kind and dates of a file or folder."""
    from core.utils import resolve_safe_path
    p = str(resolve_safe_path(path))
    if not os.path.exists(p):
        return _fail(f"I can't find {path}.")
    st = os.stat(p)
    if os.path.isdir(p):
        n = len([x for x in os.listdir(p) if not x.startswith(".")])
        kind = f"a folder with {n} items"
    else:
        kind = f"a {os.path.splitext(p)[1].lstrip('.').upper() or 'plain'} file of {st.st_size / 1e6:.1f} MB" if st.st_size >= 1e6 else \
               f"a {os.path.splitext(p)[1].lstrip('.').upper() or 'plain'} file of {max(1, st.st_size // 1000)} KB"
    mod = dt.datetime.fromtimestamp(st.st_mtime).strftime("%-d %b %Y, %-I:%M %p")
    return _ok(f"{os.path.basename(p)} is {kind}, last changed {mod}.", size=st.st_size, modified=st.st_mtime)


def recent_files(count: int = 5, days: int = 2) -> dict:
    """Your most recently changed documents (Spotlight, home folder)."""
    q = f"kMDItemContentModificationDate >= $time.today(-{max(1, int(days or 2))}) && kMDItemContentTypeTree == 'public.content'"
    out = subprocess.run(["mdfind", "-onlyin", _HOME, q], capture_output=True, text=True, timeout=10).stdout.splitlines()
    files = [p for p in out if "/Library/" not in p and "/." not in p and os.path.isfile(p)]
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    top = files[:max(1, min(int(count or 5), 20))]
    if not top:
        return _ok("No documents changed recently.", files=[])
    return _ok("Recently changed: " + ", ".join(os.path.basename(p) for p in top) + ".", files=top)


def empty_trash() -> dict:
    """Empty the Trash — gone for good (asks first)."""
    ok, _ = _osa('tell application "Finder" to empty trash', timeout=60)
    return _ok("Emptied the Trash.") if ok else _fail("I couldn't empty the Trash.")


# ── utilities ─────────────────────────────────────────────────────────

_CITY_TZ = {
    "london": "Europe/London", "paris": "Europe/Paris", "berlin": "Europe/Berlin", "madrid": "Europe/Madrid", "rome": "Europe/Rome",
    "moscow": "Europe/Moscow", "dubai": "Asia/Dubai", "delhi": "Asia/Kolkata", "new delhi": "Asia/Kolkata", "mumbai": "Asia/Kolkata",
    "india": "Asia/Kolkata", "bangalore": "Asia/Kolkata", "kolkata": "Asia/Kolkata", "singapore": "Asia/Singapore",
    "tokyo": "Asia/Tokyo", "japan": "Asia/Tokyo", "seoul": "Asia/Seoul", "beijing": "Asia/Shanghai", "shanghai": "Asia/Shanghai",
    "hong kong": "Asia/Hong_Kong", "sydney": "Australia/Sydney", "melbourne": "Australia/Melbourne", "auckland": "Pacific/Auckland",
    "new york": "America/New_York", "toronto": "America/Toronto", "chicago": "America/Chicago", "denver": "America/Denver",
    "los angeles": "America/Los_Angeles", "san francisco": "America/Los_Angeles", "seattle": "America/Los_Angeles",
    "mexico city": "America/Mexico_City", "sao paulo": "America/Sao_Paulo", "johannesburg": "Africa/Johannesburg",
    "cairo": "Africa/Cairo", "nairobi": "Africa/Nairobi", "istanbul": "Europe/Istanbul", "karachi": "Asia/Karachi",
    "dhaka": "Asia/Dhaka", "bangkok": "Asia/Bangkok", "jakarta": "Asia/Jakarta", "riyadh": "Asia/Riyadh", "utc": "UTC", "gmt": "UTC",
}


def world_time(city: str) -> dict:
    """The time right now in a city or country."""
    from zoneinfo import ZoneInfo
    key = (city or "").lower().strip()
    tz = _CITY_TZ.get(key) or next((v for k, v in _CITY_TZ.items() if k in key), None)
    if not tz:
        return _fail(f"I don't know the time zone of {city}.")
    now = dt.datetime.now(ZoneInfo(tz))
    here = dt.datetime.now().astimezone()
    diff = (now.utcoffset() - here.utcoffset()).total_seconds() / 3600
    rel = "the same as here" if diff == 0 else f"{abs(diff):g} hours {'ahead' if diff > 0 else 'behind'}"
    return _ok(f"It's {now.strftime('%-I:%M %p')} on {now.strftime('%A')} in {city.title()} — {rel}.", time=now.isoformat(timespec='minutes'))


_UNITS = {   # → base unit factor
    "length": {"m": 1, "meter": 1, "metre": 1, "km": 1000, "kilometer": 1000, "cm": .01, "mm": .001, "mile": 1609.344, "mi": 1609.344,
               "yard": .9144, "yd": .9144, "foot": .3048, "feet": .3048, "ft": .3048, "inch": .0254, "in": .0254},
    "mass": {"kg": 1, "kilogram": 1, "g": .001, "gram": .001, "mg": 1e-6, "lb": .45359237, "pound": .45359237, "oz": .028349523, "ounce": .028349523, "stone": 6.35029318},
    "volume": {"l": 1, "liter": 1, "litre": 1, "ml": .001, "gallon": 3.785411784, "gal": 3.785411784, "cup": .2365882365, "pint": .473176473, "quart": .946352946},
    "speed": {"kmh": 1, "km/h": 1, "kph": 1, "mph": 1.609344, "m/s": 3.6, "knot": 1.852},
    "data": {"b": 1, "byte": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12},
    "time": {"s": 1, "second": 1, "min": 60, "minute": 60, "h": 3600, "hour": 3600, "day": 86400, "week": 604800},
}


def convert_units(value: float, from_unit: str, to_unit: str) -> dict:
    """Convert between units: length, mass, volume, speed, data, time, temperature."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return _fail("Give a number to convert.")
    norm = lambda u: (u or "").lower().strip().rstrip("s") if (u or "").lower().strip() not in ("s", "mis", "kms", "ms") else (u or "").lower()
    f, t = norm(from_unit), norm(to_unit)
    temps = {"c": "c", "celsius": "c", "f": "f", "fahrenheit": "f", "k": "k", "kelvin": "k"}
    if f in temps and t in temps:
        c = {"c": v, "f": (v - 32) * 5 / 9, "k": v - 273.15}[temps[f]]
        out = {"c": c, "f": c * 9 / 5 + 32, "k": c + 273.15}[temps[t]]
    else:
        fam = next((d for d in _UNITS.values() if f in d and t in d), None)
        if not fam:
            return _fail(f"I can't convert {from_unit} to {to_unit}.")
        out = v * fam[f] / fam[t]
    s = f"{out:.4g}" if abs(out) < 1e6 else f"{out:,.0f}"
    return _ok(f"{v:g} {from_unit} is {s} {to_unit}.", result=out)


def define_word(word: str) -> dict:
    """The dictionary meaning of a word (the Mac's own dictionary)."""
    w = (word or "").strip()
    if not w:
        return _fail("Which word?")
    try:
        from CoreServices import DictionaryServices as D
        r = D.DCSCopyTextDefinition(None, w, (0, len(w)))
    except Exception:
        r = None
    if not r:
        return _ok(f"I couldn't find {w} in the dictionary.", found=False)
    text = re.sub(r"\s*\|[^|]*\|\s*", " ", str(r))            # drop the pronunciation
    first = re.split(r"\s(?=\d\s)|•|▶", text)[0]
    return _ok(first.strip()[:300], found=True)


def random_pick(kind: str = "coin", maximum: int = 6) -> dict:
    """Flip a coin, roll a die, or pick a number up to `maximum`."""
    k = (kind or "coin").lower()
    if "coin" in k:
        return _ok(f"It's {random.choice(['heads', 'tails'])}.")
    if "dice" in k or "die" in k:
        return _ok(f"You rolled a {random.randint(1, 6)}.")
    n = max(2, int(maximum or 6))
    return _ok(f"{random.randint(1, n)}.")


def take_screenshot(region: str = "screen") -> dict:
    """Save a screenshot to your Desktop (whole screen)."""
    name = f"Screenshot {dt.datetime.now().strftime('%Y-%m-%d at %H.%M.%S')}.png"
    path = os.path.join(_HOME, "Desktop", name)
    r = subprocess.run(["screencapture", "-x", path], capture_output=True, timeout=15)
    if r.returncode != 0 or not os.path.exists(path):
        return _fail("I couldn't take a screenshot (Screen Recording permission?).")
    return _ok(f"Saved a screenshot to your Desktop.", path=path)


def show_notification(title: str = "DEMASCAS", text: str = "") -> dict:
    """Show a macOS notification banner."""
    esc = lambda s: str(s or "").replace("\\", "\\\\").replace('"', '\\"')[:200]
    ok, _ = _osa(f'display notification "{esc(text)}" with title "{esc(title)}"')
    return _ok("Notification shown.") if ok else _fail("I couldn't show a notification.")


TOOLS = [
    get_volume, set_volume, mute, brightness, dark_mode, lock_screen, sleep_display, keep_awake,
    battery_status, disk_space, system_load, ip_address, wifi_status, set_wifi, uptime,
    media_control, now_playing, window_action, hide_app,
    create_folder, move_file, copy_file, rename_file, zip_files, reveal_in_finder, file_info, recent_files, empty_trash,
    world_time, convert_units, define_word, random_pick, take_screenshot, show_notification,
]
