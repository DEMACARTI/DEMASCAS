"""
DEMASCAS — core/llm/hardware.py
Cross-platform hardware profile used to size the local LLM and DEMASCAS's
own components (whisper / TTS threads) so the laptop never lags or overheats.

Works on macOS, Windows and Linux.  Uses psutil when installed and falls back
to OS-native sources (sysctl / vm_stat, /proc, Win32 API) otherwise.  Every
probe is bounded by short timeouts and the result is cached — profiling runs
once per process (~tens of ms), never per command.
"""

from __future__ import annotations

import functools
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field

GB = 1024 ** 3


@dataclass
class GPUInfo:
    kind: str = "none"        # apple | nvidia | none
    name: str = ""
    vram_gb: float = 0.0      # dedicated VRAM; for Apple = unified RAM
    unified: bool = False     # GPU shares system RAM (Apple Silicon)


@dataclass
class HardwareProfile:
    os: str                   # macos | windows | linux
    os_version: str
    arch: str                 # arm64 | x86_64 | ...
    cpu_name: str
    cores_physical: int
    cores_performance: int    # P-cores on hybrid CPUs; == physical otherwise
    ram_gb: float
    ram_available_gb: float
    gpu: GPUInfo = field(default_factory=GPUInfo)
    on_battery: bool | None = None
    battery_percent: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def apple_silicon(self) -> bool:
        return self.gpu.kind == "apple"

    def summary(self) -> str:
        gpu = {
            "apple": f"Apple GPU (unified {self.ram_gb:.0f} GB)",
            "nvidia": f"{self.gpu.name} ({self.gpu.vram_gb:.0f} GB VRAM)",
        }.get(self.gpu.kind, "no GPU acceleration detected (CPU inference)")
        power = ""
        if self.on_battery is not None:
            power = (f", on battery {self.battery_percent:.0f}%"
                     if self.on_battery else ", plugged in")
        return (f"{self.os} {self.os_version} {self.arch} · {self.cpu_name} · "
                f"{self.cores_physical} cores ({self.cores_performance} performance) · "
                f"{self.ram_gb:.0f} GB RAM ({self.ram_available_gb:.1f} GB free) · {gpu}{power}")


def _run(cmd: list[str], timeout: float = 2.0) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _os_name() -> str:
    return {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")


# ── memory ───────────────────────────────────────────────────────────

def _memory_gb() -> tuple[float, float]:
    """(total, available) in GB."""
    try:
        import psutil
        vm = psutil.virtual_memory()
        return vm.total / GB, vm.available / GB
    except ImportError:
        pass
    osn = _os_name()
    if osn == "macos":
        total = int(_run(["sysctl", "-n", "hw.memsize"]) or 0)
        avail = 0
        vm = _run(["vm_stat"])
        page = 16384
        m = re.search(r"page size of (\d+) bytes", vm)
        if m:
            page = int(m.group(1))
        pages = {k: int(v) for k, v in re.findall(r"Pages (free|inactive|speculative|purgeable):\s+(\d+)", vm)}
        avail = sum(pages.values()) * page
        return total / GB, avail / GB
    if osn == "linux":
        info = {}
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    k, v = line.split(":", 1)
                    info[k] = int(v.strip().split()[0]) * 1024
        except Exception:
            return 0.0, 0.0
        return info.get("MemTotal", 0) / GB, info.get("MemAvailable", info.get("MemFree", 0)) / GB
    if osn == "windows":
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return st.ullTotalPhys / GB, st.ullAvailPhys / GB
    return 0.0, 0.0


# ── CPU ──────────────────────────────────────────────────────────────

def _cpu() -> tuple[str, int, int]:
    """(name, physical cores, performance cores)."""
    osn = _os_name()
    name = platform.processor() or platform.machine()
    physical = 0
    perf = 0
    try:
        import psutil
        physical = psutil.cpu_count(logical=False) or 0
    except ImportError:
        pass
    if osn == "macos":
        name = _run(["sysctl", "-n", "machdep.cpu.brand_string"]) or name
        physical = physical or int(_run(["sysctl", "-n", "hw.physicalcpu"]) or 0)
        perf = int(_run(["sysctl", "-n", "hw.perflevel0.physicalcpu"]) or 0)
    elif osn == "linux":
        try:
            with open("/proc/cpuinfo") as f:
                txt = f.read()
            m = re.search(r"model name\s*:\s*(.+)", txt)
            if m:
                name = m.group(1).strip()
            if not physical:
                cores = {(p, c) for p, c in re.findall(r"physical id\s*:\s*(\d+).*?core id\s*:\s*(\d+)", txt, re.S)}
                physical = len(cores)
        except Exception:
            pass
    elif osn == "windows":
        name = os.environ.get("PROCESSOR_IDENTIFIER", name)
    if not physical:
        physical = max(1, (os.cpu_count() or 2) // 2)
    # Hybrid Intel/AMD P/E split isn't exposed portably — treat all as P.
    return name, physical, perf or physical


# ── GPU ──────────────────────────────────────────────────────────────

def _gpu(os_name: str, arch: str, ram_gb: float) -> GPUInfo:
    if os_name == "macos" and arch == "arm64":
        chip = _run(["sysctl", "-n", "machdep.cpu.brand_string"]) or "Apple Silicon"
        return GPUInfo(kind="apple", name=chip, vram_gb=ram_gb, unified=True)
    if shutil.which("nvidia-smi"):
        out = _run(["nvidia-smi", "--query-gpu=name,memory.total",
                    "--format=csv,noheader,nounits"], timeout=3.0)
        best = None
        for line in out.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) == 2:
                try:
                    mib = float(parts[1])
                except ValueError:
                    continue
                if best is None or mib > best[1]:
                    best = (parts[0], mib)
        if best:
            return GPUInfo(kind="nvidia", name=best[0], vram_gb=best[1] / 1024)
    # AMD/Intel GPUs: runtimes rarely accelerate them out of the box on
    # laptops, so size for CPU inference (the safe assumption for no-lag).
    return GPUInfo()


# ── power ────────────────────────────────────────────────────────────

def battery() -> tuple[bool | None, float | None]:
    """(on_battery, percent) — None when unknown / desktop."""
    try:
        import psutil
        b = psutil.sensors_battery()
        if b is None:
            return None, None
        return (not b.power_plugged), float(b.percent)
    except (ImportError, AttributeError):
        pass
    osn = _os_name()
    if osn == "macos":
        out = _run(["pmset", "-g", "batt"])
        if not out:
            return None, None
        m = re.search(r"(\d+)%", out)
        return ("Battery Power" in out), (float(m.group(1)) if m else None)
    if osn == "linux":
        base = "/sys/class/power_supply"
        try:
            for dev in os.listdir(base):
                p = os.path.join(base, dev)
                if os.path.exists(os.path.join(p, "capacity")):
                    with open(os.path.join(p, "capacity")) as f:
                        pct = float(f.read().strip())
                    status = ""
                    if os.path.exists(os.path.join(p, "status")):
                        with open(os.path.join(p, "status")) as f:
                            status = f.read().strip().lower()
                    return status == "discharging", pct
        except Exception:
            pass
        return None, None
    if osn == "windows":
        import ctypes

        class SYSTEM_POWER_STATUS(ctypes.Structure):
            _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                        ("BatteryLifePercent", ctypes.c_byte), ("SystemStatusFlag", ctypes.c_byte),
                        ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
        s = SYSTEM_POWER_STATUS()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
            if s.BatteryFlag & 128:   # no system battery
                return None, None
            pct = None if s.BatteryLifePercent == 255 else float(s.BatteryLifePercent)
            return s.ACLineStatus == 0, pct
    return None, None


# ── public API ───────────────────────────────────────────────────────

@functools.lru_cache(maxsize=1)
def _static_profile() -> HardwareProfile:
    osn = _os_name()
    arch = platform.machine().lower()
    arch = {"amd64": "x86_64", "aarch64": "arm64"}.get(arch, arch)
    if osn == "macos":
        ver = platform.mac_ver()[0]
    elif osn == "windows":
        ver = platform.version()
    else:
        ver = platform.release()
    total, avail = _memory_gb()
    cpu_name, physical, perf = _cpu()
    return HardwareProfile(
        os=osn, os_version=ver, arch=arch, cpu_name=cpu_name,
        cores_physical=physical, cores_performance=perf,
        ram_gb=round(total, 1), ram_available_gb=round(avail, 1),
        gpu=_gpu(osn, arch, round(total, 1)),
    )


def profile(refresh_dynamic: bool = True) -> HardwareProfile:
    """Hardware profile.  Static parts are cached; free RAM and battery
    are refreshed unless refresh_dynamic=False."""
    p = _static_profile()
    if refresh_dynamic:
        _, avail = _memory_gb()
        p.ram_available_gb = round(avail, 1)
        p.on_battery, p.battery_percent = battery()
    return p


def component_threads(p: HardwareProfile | None = None) -> dict:
    """Thread counts for DEMASCAS's own CPU work (whisper, Kokoro TTS).

    Leaves headroom for the LLM runtime and the user's apps so the laptop
    stays responsive.  On a 4-performance-core M1 this keeps the measured
    optimum (4 / 4)."""
    p = p or profile(refresh_dynamic=False)
    perf = max(1, p.cores_performance)
    return {
        "whisper_threads": max(2, min(perf, 8)),
        "tts_threads": max(2, min(perf, 4)),
    }
