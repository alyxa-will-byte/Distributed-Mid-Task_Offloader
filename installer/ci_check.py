"""Full install check on a real machine (CI runs it on Windows, macOS and Linux):
build the site files, serve them, run the real installer with a pairing code, check the background service brings the
helper online, then remove it from the relay and check it wipes itself. Uses a throwaway relay and AXR_HOME.

    python installer/ci_check.py
"""
from __future__ import annotations

import http.server
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait(fn, what, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if fn():
                return
        except Exception:
            pass
        time.sleep(1)
    raise SystemExit(f"FAIL: timed out waiting for {what}")


def win_autostart() -> str | None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
            return winreg.QueryValueEx(k, "axr-worker")[0]
    except OSError:
        return None


def main() -> int:
    subprocess.run([sys.executable, str(ROOT / "installer" / "build.py")], check=True)
    site = ROOT / "dist" / "site"
    web = port()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", web), partial(http.server.SimpleHTTPRequestHandler, directory=str(site)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    tmp = Path(tempfile.mkdtemp(prefix="axr-ci-"))
    rport, key = port(), secrets.token_hex(24)
    relay = subprocess.Popen(["node", str(ROOT / "relay" / "relay.mjs"), "serve"], env={**os.environ, "RELAY_PORT": str(rport), "RELAY_DATA": str(tmp / "relay"), "RELAY_ADMIN_KEY": key})

    def api(method, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{rport}{path}", method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"authorization": f"Bearer {key}", "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    try:
        wait(lambda: api("GET", "/health")["ok"], "relay")
        code = api("POST", "/admin/pair", {"role": "worker"})["code"]
        home = tmp / "axr-home"
        env = {**os.environ, "AXR_BASE": f"http://127.0.0.1:{web}", "AXR_CODE": code, "AXR_RELAY": f"ws://127.0.0.1:{rport}",
               "AXR_HOME": str(home), "AXR_NO_KEYRING": "1", "AXR_NO_ANIM": "1"}
        t = time.time()
        if sys.platform == "win32":
            cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", f"Get-Content -Raw '{site / 'install.ps1'}' | Invoke-Expression"]
        else:
            cmd = ["sh", str(site / "install.sh")]
        r = subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL, timeout=600)
        print(f"installer exit {r.returncode} after {time.time() - t:.1f}s")
        if r.returncode:
            raise SystemExit("FAIL: installer failed")
        if not (home / "identity.json").exists():
            raise SystemExit("FAIL: not paired")
        wait(lambda: any(d["online"] for d in api("GET", "/admin/devices")["devices"]), "the background service to bring the helper online")
        if sys.platform == "win32":
            if not win_autostart():
                raise SystemExit("FAIL: the helper is not set to start at sign-in (HKCU Run key missing)")
            print("PASS: set to start at sign-in")
        dev = api("GET", "/admin/devices")["devices"][0]
        print(f"PASS: installed, paired and online as {dev['name']} ({dev['os']}) in {time.time() - t:.1f}s")

        api("POST", f"/admin/devices/{dev['id']}/remove")
        wait(lambda: not (home / "identity.json").exists(), "the removed helper to wipe itself", timeout=30)
        print("PASS: removed from the relay and the helper wiped its keys")
        if sys.platform == "win32":
            wait(lambda: not win_autostart(), "the sign-in entry to be removed", timeout=30)
            print("PASS: sign-in entry removed")
        return 0
    finally:
        relay.terminate()
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
