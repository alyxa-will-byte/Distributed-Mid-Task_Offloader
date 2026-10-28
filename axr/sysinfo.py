"""What a device can do right now: load, size and the tools it has. Sent to the relay so the laptop can pick a helper."""
from __future__ import annotations

import platform
import shutil
import sys

import psutil

# Commands whose presence decides where a job can run. A job whose program is in this list only goes to devices that have it.
KNOWN_TOOLS = ("python", "python3", "node", "npm", "ffmpeg", "blender", "uv", "docker", "nvidia-smi", "gcc", "make", "cargo", "go", "java", "bash", "pwsh")


def os_name() -> str:
    return {"win32": "windows", "darwin": "mac"}.get(sys.platform, "linux" if sys.platform.startswith("linux") else sys.platform)


def tools() -> list[str]:
    found = [t for t in KNOWN_TOOLS if shutil.which(t)]
    # The worker can always run Python jobs with its own interpreter.
    for t in ("python", "python3"):
        if t not in found:
            found.append(t)
    return sorted(found)


def status(jobs: int = 0, max_jobs: int = 1, paused: bool = False) -> dict:
    vm = psutil.virtual_memory()
    return {
        "cpu": psutil.cpu_percent(interval=None),
        "mem": vm.percent,
        "mem_free_gb": round(vm.available / 2**30, 1),
        "cores": psutil.cpu_count() or 1,
        "jobs": jobs,
        "max_jobs": max_jobs,
        "paused": paused,
        "os": os_name(),
        "arch": platform.machine().lower(),
        "python": platform.python_version(),
        "tools": tools(),
    }


def local_busy(cpu_limit: float = 75.0, mem_limit: float = 85.0) -> bool:
    return psutil.cpu_percent(interval=0.5) >= cpu_limit or psutil.virtual_memory().percent >= mem_limit
