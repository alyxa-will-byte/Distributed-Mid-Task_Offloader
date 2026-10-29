"""Remote desktop (RustDesk, on your own server) for helpers.

`axr desktop setup <helper>` on the controller asks the helper to download RustDesk, show ONE admin prompt on its own
screen, install it as a service pointed at your server, and set a random permanent password. The helper sends the ID and
password back over the end-to-end sealed channel; the controller keeps them in desktops.json (only you can read it).
`axr desktop <helper>` then opens the session with no typing.

Unlinking the helper (axr devices remove / axr leave) also turns its remote desktop off: setup installs a small SYSTEM
task that checks every 5 minutes whether this helper is still paired, and if not rotates the password and stops RustDesk.

Windows helpers only for now: on macOS and Linux, RustDesk needs sudo or a manual privacy approval.
"""
from __future__ import annotations

import json
import os
import platform
import re
import secrets
import shutil
import string
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

RELEASES = "https://api.github.com/repos/rustdesk/rustdesk/releases/latest"
DEFAULT_HOST = "rd.alyxrbytes.pro"
GUARD_TASK = "axr-desktop-guard"
UNSUPPORTED = "remote desktop setup works on Windows helpers only for now"


def config_string(host: str, key: str) -> str:
    """The form `rustdesk --config` reads: host=...,key=...,relay=..., (trailing comma is RustDesk's delimiter)."""
    for v in (host, key):
        if not v or "," in v or '"' in v or "'" in v:
            raise ValueError("server and key must be non-empty and contain no commas or quotes")
    return f"host={host},key={key},relay={host},"


def new_password(n: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _ps(s: str) -> str:
    """A PowerShell single-quoted literal."""
    return "'" + str(s).replace("'", "''") + "'"


def guard_script(identity: Path) -> str:
    return f"""# axr: turn remote desktop off once this helper is no longer paired (runs as SYSTEM every 5 minutes)
if (-not (Test-Path -LiteralPath {_ps(identity)})) {{
  $exe = Join-Path $env:ProgramFiles 'RustDesk\\rustdesk.exe'
  if (Test-Path $exe) {{ & $exe --password ([guid]::NewGuid().ToString('N')) | Out-Null }}
  Stop-Service -Name RustDesk -ErrorAction SilentlyContinue
  Set-Service -Name RustDesk -StartupType Disabled -ErrorAction SilentlyContinue
  schtasks /Delete /TN {GUARD_TASK} /F | Out-Null
}}
"""


def setup_script(*, installer: Path, config: str, password: str | None, result: Path, guard: tuple[Path, str] | None) -> str:
    """The script that runs elevated (after the one admin prompt). Mirrors RustDesk's own deployment script."""
    pw = f"""
  $out = (& $exe --password {_ps(password)} | Out-String).Trim()
  if ($out -notmatch 'Done') {{ throw "setting the password failed: $out" }}""" if password else ""
    g = ""
    if guard:
        path, text = guard
        g = f"""
  New-Item -ItemType Directory -Force -Path {_ps(path.parent)} | Out-Null
  Set-Content -LiteralPath {_ps(path)} -Value {_ps(text)} -Encoding ASCII
  schtasks /Create /TN {GUARD_TASK} /RU SYSTEM /SC MINUTE /MO 5 /TR ('powershell -NoProfile -ExecutionPolicy Bypass -File "' + {_ps(path)} + '"') /F | Out-Null"""
    return f"""$ErrorActionPreference = 'Stop'
try {{
  $exe = Join-Path $env:ProgramFiles 'RustDesk\\rustdesk.exe'
  if (-not (Test-Path $exe)) {{
    # Do not wait on the process: that also waits for everything it starts, and RustDesk starts itself and keeps running.
    Start-Process -FilePath {_ps(installer)} -ArgumentList '--silent-install'
    for ($i = 0; $i -lt 90 -and -not (Test-Path $exe); $i++) {{ Start-Sleep -Seconds 2 }}
    if (-not (Test-Path $exe)) {{ throw 'RustDesk did not install' }}
  }}
  $svc = Get-Service -Name RustDesk -ErrorAction SilentlyContinue
  if (-not $svc) {{ Start-Process -FilePath $exe -ArgumentList '--install-service'
    for ($i = 0; $i -lt 30 -and -not (Get-Service -Name RustDesk -ErrorAction SilentlyContinue); $i++) {{ Start-Sleep -Seconds 2 }} }}
  Set-Service -Name RustDesk -StartupType Automatic
  for ($i = 0; $i -lt 30 -and (Get-Service -Name RustDesk).Status -ne 'Running'; $i++) {{
    Start-Service -Name RustDesk -ErrorAction SilentlyContinue; Start-Sleep -Seconds 2 }}
  & $exe --config {_ps(config)} | Out-Null{pw}
  $id = ''
  for ($i = 0; $i -lt 30 -and -not $id; $i++) {{ $id = (& $exe --get-id | Out-String).Trim(); if (-not $id) {{ Start-Sleep -Seconds 2 }} }}
  if (-not $id) {{ throw 'RustDesk is installed but did not report an ID' }}{g}
  $r = @{{ ok = $true; id = $id }}
}} catch {{
  $r = @{{ ok = $false; error = $_.Exception.Message }}
}}
$r | ConvertTo-Json -Compress | Set-Content -LiteralPath {_ps(result)} -Encoding UTF8
"""


def _arch() -> str:
    return "aarch64" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"


def download_installer(dest_dir: Path) -> Path:
    headers = {"User-Agent": "axr-offload", "Accept": "application/vnd.github+json"}
    if os.environ.get("AXR_GITHUB_TOKEN"):   # optional: avoids GitHub's anonymous rate limit (used in CI)
        headers["Authorization"] = f"Bearer {os.environ['AXR_GITHUB_TOKEN']}"
    req = urllib.request.Request(RELEASES, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        rel = json.load(r)
    want = re.compile(rf"^rustdesk-[\d.]+-{_arch()}\.exe$")
    asset = next((a for a in rel.get("assets", []) if want.match(a.get("name", ""))), None)
    if not asset:
        raise RuntimeError(f"no RustDesk Windows installer for {_arch()} in release {rel.get('tag_name')}")
    dest = dest_dir / asset["name"]
    with urllib.request.urlopen(urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": "axr-offload"}), timeout=300) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    if dest.stat().st_size != asset.get("size", dest.stat().st_size):
        raise RuntimeError("the RustDesk download is incomplete")
    return dest


def run_elevated(script: Path) -> None:
    """Shows the Windows admin prompt (UAC) on this machine's screen and starts the script if someone allows it."""
    import ctypes

    args = f'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{script}"'
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe", args, None, 0)
    if rc <= 32:
        raise PermissionError("the admin prompt was declined" if rc == 5 else f"could not start the installer (error {rc})")


def wait_for(result: Path, timeout: float = 900) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        if result.exists():
            try:
                return json.loads(result.read_text(encoding="utf-8-sig"))
            except ValueError:
                pass   # still being written
        time.sleep(1)
    raise TimeoutError("RustDesk setup did not finish in time")


def install(*, host: str, key: str, with_password: bool, identity: Path | None, progress=lambda s: None) -> dict:
    """Downloads RustDesk, asks for admin once, installs and configures it. Returns {"id", "password"}. Windows only."""
    if sys.platform != "win32":
        raise RuntimeError(UNSUPPORTED)
    cfg = config_string(host, key)
    password = new_password() if with_password else None
    work = Path(tempfile.mkdtemp(prefix="axr-desktop-"))
    try:
        exe = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "RustDesk" / "rustdesk.exe"
        installer = exe
        if not exe.exists():
            progress("Downloading RustDesk")
            installer = download_installer(work)
        guard = None
        if identity is not None:
            guard = (Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "axr" / "desktop-guard.ps1", guard_script(identity))
        result = work / "result.json"
        script = work / "setup.ps1"
        script.write_text(setup_script(installer=installer, config=cfg, password=password, result=result, guard=guard), encoding="utf-8-sig")
        progress("Waiting for someone to click Yes on the admin prompt")
        run_elevated(script)
        progress("Installing and configuring RustDesk")
        r = wait_for(result)
        if not r.get("ok"):
            raise RuntimeError(r.get("error") or "RustDesk setup failed")
        return {"id": str(r["id"]), "password": password}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# -- controller side -------------------------------------------------------------------------------------------------
def viewer_path() -> Path | None:
    cands = []
    if sys.platform == "win32":
        cands = [Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "RustDesk" / "rustdesk.exe"]
    elif sys.platform == "darwin":
        cands = [Path("/Applications/RustDesk.app/Contents/MacOS/RustDesk")]
    found = shutil.which("rustdesk")
    cands += [Path(found)] if found else []
    return next((p for p in cands if p.exists()), None)


def open_session(exe: Path, rid: str, password: str) -> None:
    kw = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {"start_new_session": True}
    subprocess.Popen([str(exe), "--connect", rid, "--password", password], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, **kw)


class Store:
    """desktops.json in the controller's axr folder: {helper_id: {"name", "rustdesk_id", "password", "host"}}."""

    def __init__(self, home: Path):
        self.file = Path(home) / "desktops.json"

    def all(self) -> dict:
        try:
            return json.loads(self.file.read_text())
        except (OSError, ValueError):
            return {}

    def put(self, helper_id: str, entry: dict) -> None:
        d = self.all()
        d[helper_id] = entry
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, self.file)

    def find(self, ref: str) -> tuple[str, dict] | None:
        hits = [(k, v) for k, v in self.all().items() if k.startswith(ref) or v.get("name", "").lower() == ref.lower()]
        return hits[0] if len(hits) == 1 else None
