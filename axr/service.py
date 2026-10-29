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
WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
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
        # The per-user Run key needs no admin rights (an ONLOGON scheduled task does, so it failed for normal accounts).
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY) as k:
            winreg.SetValueEx(k, NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(cmd))
        subprocess.run(["schtasks", "/Delete", "/TN", NAME, "/F"], capture_output=True)   # an older install's task, if any
        start_detached(cmd, home / "worker.log")
        return "Set the helper to start whenever you sign in (no admin needed) and started it now, with no window."
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
<key>RunAtLoad</key><true/><key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
<key>StandardOutPath</key><string>{log}</string><key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
""")
        subprocess.run(["launchctl", "unload", str(p)], capture_output=True)
        subprocess.run(["launchctl", "load", "-w", str(p)], check=True, capture_output=True)
        return f"Installed a LaunchAgent ({p}); the helper starts at login and restarts if it crashes."
    u = _systemd_unit()
    u.parent.mkdir(parents=True, exist_ok=True)
    u.write_text(f"""[Unit]
Description=axr helper (runs tasks sent from your laptop)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart={' '.join(_q(a) for a in cmd)}
Restart=on-failure
RestartSec=5
Nice=5

[Install]
WantedBy=default.target
""")
    ok = shutil.which("systemctl") and subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True).returncode == 0 \
        and subprocess.run(["systemctl", "--user", "enable", "--now", f"{NAME}.service"], capture_output=True).returncode == 0
    if not ok:   # no systemd user session (headless server, WSL, container): start at boot from cron instead
        u.unlink(missing_ok=True)
        return _install_cron(cmd, home)
    linger = ""
    if shutil.which("loginctl"):
        r = subprocess.run(["loginctl", "enable-linger", os.environ.get("USER", "")], capture_output=True)
        linger = " It also runs when nobody is logged in." if r.returncode == 0 else " To run it without logging in: sudo loginctl enable-linger $USER"
    return f"Installed a systemd user service ({u}).{linger}"


CRON_TAG = "# axr-worker"


def _crontab() -> list[str]:
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return r.stdout.splitlines() if r.returncode == 0 else []


def _install_cron(cmd: list[str], home: Path) -> str:
    log = home / "worker.log"
    if not shutil.which("crontab"):
        start_detached(cmd, log)
        return ("Started the helper now. This machine has no systemd user session or cron, so it will not start by itself "
                "after a reboot: add 'axr worker start' to this machine's startup.")
    lines = [ln for ln in _crontab() if CRON_TAG not in ln]
    lines.append(f"@reboot {' '.join(_q(a) for a in cmd)} >>{_q(str(log))} 2>&1 {CRON_TAG}")
    subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True, capture_output=True)
    start_detached(cmd, log)
    return "Set the helper to start at boot (cron) and started it now."


def start_detached(cmd: list[str], log: Path):
    """Starts the helper in the background now; it outlives the window that started it.
    A second copy exits on its own (see worker.single_instance)."""
    log.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        # The helper logs to worker.log itself; no console, and not tied to the installer's window.
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
        return
    with open(log, "ab") as out:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True, close_fds=True)


def _q(a: str) -> str:
    return f'"{a}"' if any(c in a for c in ' \t"\\') else a


def uninstall(quiet: bool = False) -> str:
    run = lambda *a: subprocess.run(list(a), capture_output=True)  # noqa: E731
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, NAME)
        except OSError:
            pass
        run("schtasks", "/Delete", "/TN", NAME, "/F")
        return "Removed the helper from Windows sign-in."
    if sys.platform == "darwin":
        p = _plist()
        if p.exists():
            run("launchctl", "unload", "-w", str(p))
            p.unlink(missing_ok=True)
        return "Removed the LaunchAgent."
    if shutil.which("crontab"):
        lines = _crontab()
        if any(CRON_TAG in ln for ln in lines):
            subprocess.run(["crontab", "-"], input="\n".join(ln for ln in lines if CRON_TAG not in ln) + "\n", text=True, capture_output=True)
    u = _systemd_unit()
    if u.exists():
        u.unlink(missing_ok=True)
        if shutil.which("systemctl"):
            run("systemctl", "--user", "daemon-reload")
            # Last: this stops the service, which may be this very process.
            run("systemctl", "--user", "disable", "--now", "--no-block", f"{NAME}.service")
    return "Removed the systemd user service."
