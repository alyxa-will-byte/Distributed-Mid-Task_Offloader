"""Spins up a real relay (node relay/relay.mjs) and real worker processes for end-to-end tests."""
from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(fn, timeout=20.0, every=0.2, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        time.sleep(every)
    raise AssertionError(f"timed out waiting for {what}")


class Relay:
    def __init__(self, data: Path):
        self.data, self.port, self.key, self.proc = data, free_port(), secrets.token_hex(24), None
        self.url = f"ws://127.0.0.1:{self.port}"

    def start(self):
        env = {**os.environ, "RELAY_PORT": str(self.port), "RELAY_DATA": str(self.data), "RELAY_ADMIN_KEY": self.key}
        self.proc = subprocess.Popen(["node", str(ROOT / "relay" / "relay.mjs"), "serve"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        wait_for(lambda: self.http("GET", "/health")["ok"], what="relay to start")

    def stop(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(10)
            self.proc = None

    def http(self, method, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"authorization": f"Bearer {self.key}", "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    def code(self, role="worker"):
        return self.http("POST", "/admin/pair", {"role": role})["code"]

    def devices(self):
        return {d["name"]: d for d in self.http("GET", "/admin/devices")["devices"]}


ENV = {**os.environ, "AXR_NO_KEYRING": "1", "PYTHONUNBUFFERED": "1"}


def axr(home: Path, *args, cwd=None, timeout=60, check=False):
    r = subprocess.run([sys.executable, "-m", "axr", "--home", str(home), *args], cwd=cwd, env=ENV, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise AssertionError(f"axr {args} failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
    return r


class Worker:
    def __init__(self, home: Path):
        self.home, self.proc = home, None

    def start(self):
        self.proc = subprocess.Popen([sys.executable, "-m", "axr", "--home", str(self.home), "worker", "start"], env=ENV,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


@pytest.fixture
def relay(tmp_path):
    r = Relay(tmp_path / "relay-data")
    r.start()
    yield r
    r.stop()


@pytest.fixture
def mesh(relay, tmp_path):
    """A paired laptop (controller) and one running, paired helper called 'helper1'."""
    laptop, helper = tmp_path / "laptop", tmp_path / "helper1"
    axr(laptop, "join", relay.code("controller"), "--relay", relay.url, "--name", "laptop", check=True)
    axr(helper, "join", relay.code("worker"), "--relay", relay.url, "--name", "helper1", "--no-service", "--max-jobs", "1", check=True)
    w = Worker(helper)
    w.start()
    wait_for(lambda: relay.devices()["helper1"]["online"] and relay.devices()["helper1"]["status"], what="helper online")
    yield {"relay": relay, "laptop": laptop, "helper": helper, "worker": w, "tmp": tmp_path}
    w.stop()


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "project"
    p.mkdir()
    (p / "data.txt").write_text("21313\n")
    (p / "job.py").write_text(
        "import os, sys, platform\n"
        "n = int(open('data.txt').read())\n"
        "print('hello from', os.environ.get('AXR_JOB', 'local'))\n"
        "print('to stderr', file=sys.stderr)\n"
        "os.makedirs('out', exist_ok=True)\n"
        "open('out/result.txt', 'w').write(str(n * 2))\n"
        "sys.exit(int(sys.argv[1]) if len(sys.argv) > 1 else 0)\n")
    (p / ".git").mkdir()
    (p / ".git" / "secret").write_text("not sent")
    return p

