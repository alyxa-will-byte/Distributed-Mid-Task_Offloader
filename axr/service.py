"""Make the helper start by itself and stay running: a logon task on Windows, a systemd user unit on Linux,
a LaunchAgent on macOS. The worker reconnects after sleep on its own, so nothing extra is needed for wake."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .identity import default_home

NAME = "axr-worker"
LABEL = "pro.alyxrbytes.axr-worker"


def _cmd(home: Path) -> list[str]:
    exe = sys.executable
    if sys.platform == "win32":
        w = Path(exe).with_name("pythonw.exe")   # no console window
        exe = str(w) if w.exists() else exe
    return [exe, "-m", "axr", "--home", str(home), "worker", "start"]


def _systemd_unit() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user" / f"{NAME}.service"


def _plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def install(home: Path | None = None) -> str:
    home = Path(home or default_home())
    cmd = _cmd(home)
    if sys.platform == "win32":
        tr = subprocess.list2cmdline(cmd)
        subprocess.run(["schtasks", "/Create", "/TN", NAME, "/TR", tr, "/SC", "ONLOGON", "/RL", "LIMITED", "/F"], check=True, capture_output=True)
        subprocess.run(["schtasks", "/Run", "/TN", NAME], capture_output=True)
        return "Installed a Windows task that starts the helper when you sign in (and started it now)."
    if sys.platform == "darwin":
        p = _plist()
        p.parent.mkdir(parents=True, exist_ok=True)
        args = "".join(f"<string>{a}</string>" for a in cmd)
        log = home / "worker.log"
        p.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>{LABEL}</string>
<key>ProgramArguments</key><array>{args}</array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>StandardOutPath</key><string>{log}</string><key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
""")
        subprocess.run(["launchctl", "unload", str(p)], capture_output=True)
        subprocess.run(["launchctl", "load", "-w", str(p)], check=True, capture_output=True)
        return f"Installed a LaunchAgent ({p}); the helper starts at login and restarts if it stops."
    u = _systemd_unit()
    u.parent.mkdir(parents=True, exist_ok=True)
    u.write_text(f"""[Unit]
Description=axr helper (runs tasks sent from your laptop)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart={' '.join(_q(a) for a in cmd)}
Restart=always
RestartSec=5
Nice=5

[Install]
WantedBy=default.target
""")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", f"{NAME}.service"], check=True)
    linger = ""
    if shutil.which("loginctl"):
        r = subprocess.run(["loginctl", "enable-linger", os.environ.get("USER", "")], capture_output=True)
        linger = " It also runs when nobody is logged in." if r.returncode == 0 else " To run it without logging in: sudo loginctl enable-linger $USER"
    return f"Installed a systemd user service ({u}).{linger}"


def _q(a: str) -> str:
    return f'"{a}"' if any(c in a for c in ' \t"\\') else a


def uninstall(quiet: bool = False) -> str:
    run = lambda *a: subprocess.run(list(a), capture_output=True)  # noqa: E731
    if sys.platform == "win32":
        run("schtasks", "/Delete", "/TN", NAME, "/F")
        return "Removed the Windows task."
    if sys.platform == "darwin":
        p = _plist()
        if p.exists():
            run("launchctl", "unload", "-w", str(p))
            p.unlink(missing_ok=True)
        return "Removed the LaunchAgent."
    u = _systemd_unit()
    if u.exists():
        u.unlink(missing_ok=True)
        if shutil.which("systemctl"):
            run("systemctl", "--user", "daemon-reload")
            # Last: this stops the service, which may be this very process.
            run("systemctl", "--user", "disable", "--now", "--no-block", f"{NAME}.service")
    return "Removed the systemd user service."
