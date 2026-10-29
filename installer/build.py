"""Build the wheel and the two install scripts (with the wheel's SHA-256 baked in) into dist/site/.

Upload the contents of dist/site/ to https://alyxrbytes.pro/axr/ (the alyxrbytes.pro repo serves them):
    python installer/build.py
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    out = ROOT / "dist" / "site"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(out)], cwd=ROOT, check=True)
    wheel = out / f"axr_offload-{version}-py3-none-any.whl"
    if not wheel.exists():
        sys.exit(f"expected {wheel.name}")
    sha = hashlib.sha256(wheel.read_bytes()).hexdigest()
    for name in ("install.sh", "install.ps1"):
        text = (ROOT / "installer" / f"{name}.in").read_text().replace("@VERSION@", version).replace("@SHA256@", sha)
        # Windows PowerShell 5.1 reads scripts without a BOM as the local code page; the scripts stay ASCII to be safe.
        text.encode("ascii")
        (out / name).write_bytes(text.replace("\r\n", "\n").encode() if name.endswith(".sh") else text.replace("\r\n", "\n").replace("\n", "\r\n").encode())
    print(f"built {wheel.name}  sha256 {sha}\n  -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
