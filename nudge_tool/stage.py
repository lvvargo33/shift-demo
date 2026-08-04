"""Live progress markers for the cron jobs: one line per stage, on stderr.

WHY THIS EXISTS (2026-08-04). The cron scripts collect their whole report in a
StringIO via contextlib.redirect_stdout and print it only at the very end, so a
container killed mid-run leaves an EMPTY Render log AND sends no report email.
That is exactly what happened when shift-nudge-send was OOM-killed at 512 MB:
the only evidence was Render's own "Out of memory" alert, with no way to tell
which stage was running.

stderr is NOT redirected, so these markers reach the Render log as they happen.
If the job is killed, the LAST marker names the stage that was running.

Each line carries elapsed seconds and memory, e.g.

    [send]   12.3s rss=187MB peak=190MB | ingest.load done (6661 climbers)

peak is the high-water mark for the whole process, so even a run that SUCCEEDS
now reports how close it came to the instance ceiling. That is the number to
watch: if peak creeps toward the plan's limit, act before it fails.

Memory comes from /proc/self/status (Linux, i.e. Render). Local Windows runs
fall back to the Win32 working set, and to no memory at all if neither works:
the timeline must never depend on the memory read succeeding.
"""
from __future__ import annotations

import sys
import time

_T0 = time.monotonic()


def _linux_mem() -> tuple[float, float] | None:
    """(rss_mb, peak_rss_mb) from /proc/self/status. None off Linux."""
    try:
        rss = peak = None
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) / 1024
                elif line.startswith("VmHWM:"):
                    peak = int(line.split()[1]) / 1024
        if rss is None:
            return None
        return rss, (peak if peak is not None else rss)
    except (OSError, ValueError, IndexError):
        return None


def _windows_mem() -> tuple[float, float] | None:
    """(working_set_mb, peak_working_set_mb) via Win32. None off Windows."""
    try:
        import ctypes
        import ctypes.wintypes as wt

        class _PMC(ctypes.Structure):
            _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32")
        fn = getattr(k32, "K32GetProcessMemoryInfo", None) \
            or ctypes.WinDLL("psapi").GetProcessMemoryInfo
        fn.argtypes = [wt.HANDLE, ctypes.POINTER(_PMC), wt.DWORD]
        fn.restype = wt.BOOL
        c = _PMC()
        c.cb = ctypes.sizeof(c)
        if not fn(k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
            return None
        return c.WorkingSetSize / 2**20, c.PeakWorkingSetSize / 2**20
    except Exception:  # noqa: BLE001 - a diagnostic must never raise
        return None


def memory() -> tuple[float, float] | None:
    return _linux_mem() or _windows_mem()


def stage(label: str, tag: str = "send") -> None:
    """Print one progress marker to stderr. Never raises: a failure here must
    not take down the run it is only supposed to describe."""
    try:
        mem = memory()
        part = ""
        if mem:
            part = f" rss={mem[0]:.0f}MB peak={mem[1]:.0f}MB"
        print(f"[{tag}] {time.monotonic() - _T0:6.1f}s{part} | {label}",
              file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass


def versions() -> str:
    """Installed versions of the libraries the cron depends on. requirements.txt
    is unpinned, so a redeploy can silently change any of these; printing them
    every run means a behaviour change is traceable to the version that caused
    it (and gives the exact list to pin once a run is known good)."""
    out = []
    try:
        from importlib.metadata import PackageNotFoundError, version
        for name in ("requests", "python-dotenv", "google-auth",
                     "google-api-python-client", "httplib2"):
            try:
                out.append(f"{name}=={version(name)}")
            except PackageNotFoundError:
                out.append(f"{name}==?")
    except Exception:  # noqa: BLE001
        return ""
    return " ".join(out)
