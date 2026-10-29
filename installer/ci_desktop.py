"""Real remote-desktop check on a Windows machine (CI runs it on windows-latest, where the runner is already admin so the
admin prompt is approved by itself). Pairs a controller and a helper against a throwaway relay, runs
`axr desktop setup`, checks RustDesk is installed, running and pointed at our server, then removes the helper and checks
the guard task turns RustDesk off.   python installer/ci_desktop.py
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXE = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "RustDesk" / "rustdesk.exe"


def port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait(fn, what, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if fn():
                return
        except Exception:
            pass
        time.sleep(2)
    raise SystemExit(f"FAIL: timed out waiting for {what}")


def sh(*cmd) -> str:
    return subprocess.run(list(cmd), capture_output=True, text=True, errors="replace").stdout


def main() -> int:
    if sys.platform != "win32":
        raise SystemExit("this check is for Windows")
    tmp = Path(tempfile.mkdtemp(prefix="axr-desk-"))
    rport, key = port(), secrets.token_hex(24)
    relay = subprocess.Popen(["node", str(ROOT / "relay" / "relay.mjs"), "serve"],
                             env={**os.environ, "RELAY_PORT": str(rport), "RELAY_DATA": str(tmp / "relay"), "RELAY_ADMIN_KEY": key})
    env = {**os.environ, "AXR_NO_KEYRING": "1", "AXR_NO_ANIM": "1", "PYTHONIOENCODING": "utf-8"}
    url = f"ws://127.0.0.1:{rport}"

    def api(method, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{rport}{path}", method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"authorization": f"Bearer {key}", "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def axr(home, *args, timeout=120):
        r = subprocess.run([sys.executable, "-m", "axr", "--home", str(home), *args], env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        print(r.stdout + r.stderr)
        return r

    worker = None
    try:
        wait(lambda: api("GET", "/health")["ok"], "relay")
        laptop, helper = tmp / "laptop", tmp / "helper"
        assert axr(laptop, "join", api("POST", "/admin/pair", {"role": "controller"})["code"], "--relay", url, "--name", "laptop").returncode == 0
        assert axr(helper, "join", api("POST", "/admin/pair", {"role": "worker"})["code"], "--relay", url, "--name", "deskhelper", "--no-service").returncode == 0
        worker = subprocess.Popen([sys.executable, "-m", "axr", "--home", str(helper), "worker", "start"], env=env)
        wait(lambda: any(d["online"] and d["status"] for d in api("GET", "/admin/devices")["devices"] if d["name"] == "deskhelper"), "helper online")

        t = time.time()
        r = axr(laptop, "desktop", "setup", "deskhelper", "--server", "127.0.0.1", "--key", "testkey123", timeout=1500)
        if r.returncode != 0:
            raise SystemExit("FAIL: axr desktop setup failed")
        entry = json.loads((laptop / "desktops.json").read_text())
        rid = next(iter(entry.values()))["rustdesk_id"]
        print(f"PASS: RustDesk installed and configured in {time.time() - t:.0f}s, ID {rid}")

        if "RUNNING" not in sh("sc", "query", "RustDesk"):
            raise SystemExit("FAIL: the RustDesk service is not running")
        server = sh(str(EXE), "--option", "custom-rendezvous-server").strip()
        if server != "127.0.0.1":
            raise SystemExit(f"FAIL: RustDesk server is {server!r}, expected 127.0.0.1")
        if "axr-desktop-guard" not in sh("schtasks", "/Query", "/TN", "axr-desktop-guard"):
            raise SystemExit("FAIL: the guard task was not created")
        print("PASS: service running, pointed at our server, guard task installed")

        dev = next(d for d in api("GET", "/admin/devices")["devices"] if d["name"] == "deskhelper")
        api("POST", f"/admin/devices/{dev['id']}/remove")
        wait(lambda: not (helper / "identity.json").exists(), "the removed helper to wipe itself", timeout=60)
        sh("schtasks", "/Run", "/TN", "axr-desktop-guard")
        wait(lambda: "STOPPED" in sh("sc", "query", "RustDesk") and "DISABLED" in sh("sc", "qc", "RustDesk"), "the guard to turn RustDesk off")
        print("PASS: removing the helper turned its remote desktop off")
        return 0
    finally:
        if worker and worker.poll() is None:
            worker.kill()
        relay.terminate()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
