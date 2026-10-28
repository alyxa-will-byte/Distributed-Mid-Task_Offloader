"""Pack a project folder to send with a job, and unpack results safely. Nothing gets installed anywhere: the folder
is unpacked into a throwaway workspace on the helper and deleted when the job ends."""
from __future__ import annotations

import base64
import fnmatch
import io
import os
import tarfile
from pathlib import Path, PurePosixPath

MAX_BUNDLE = 200 * 1024 * 1024
CHUNK = 512 * 1024
DEFAULT_IGNORE = [".git", ".hg", ".svn", "__pycache__", "*.pyc", "node_modules", ".venv", "venv", "env", ".mypy_cache",
                  ".pytest_cache", ".DS_Store", "Thumbs.db", "out", ".axr"]


class BundleError(Exception):
    pass


def ignore_patterns(root: Path) -> list[str]:
    pats = list(DEFAULT_IGNORE)
    f = root / ".axrignore"
    if f.is_file():
        pats += [ln.strip().rstrip("/") for ln in f.read_text(errors="replace").splitlines() if ln.strip() and not ln.startswith("#")]
    return pats


def _ignored(rel: str, pats: list[str]) -> bool:
    parts = rel.split("/")
    return any(fnmatch.fnmatch(p, pat) for p in parts for pat in pats) or any(fnmatch.fnmatch(rel, pat) for pat in pats)


def pack(root: Path | str, *, limit: int = MAX_BUNDLE, only: str | None = None) -> bytes:
    """tar.gz of root (or root/only), skipping ignored files and anything that is not a plain file or folder."""
    root = Path(root).resolve()
    base = (root / only) if only else root
    pats = ignore_patterns(root)
    buf = io.BytesIO()
    total = 0
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=6) as tar:
        if not base.exists():
            return buf.getvalue()
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            d = Path(dirpath)
            rel_d = d.relative_to(base).as_posix()
            dirnames[:] = [n for n in dirnames if not (only is None and _ignored(f"{rel_d}/{n}".lstrip("./"), pats)) and not (d / n).is_symlink()]
            for n in filenames:
                p = d / n
                rel = f"{rel_d}/{n}".lstrip("./") if rel_d != "." else n
                if p.is_symlink() or not p.is_file() or (only is None and _ignored(rel, pats)):
                    continue
                total += p.stat().st_size
                if total > limit:
                    raise BundleError(f"the folder is over {limit // 2**20} MB; add big files to .axrignore")
                tar.add(p, arcname=rel, recursive=False)
    return buf.getvalue()


def safe_extract(data: bytes, dest: Path | str, *, limit: int = MAX_BUNDLE * 2) -> list[str]:
    """Unpack only plain files and folders that stay inside dest. Returns the file paths written."""
    dest = Path(dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    written, total = [], 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar:
            name = m.name.replace("\\", "/")
            pp = PurePosixPath(name)
            if pp.is_absolute() or ".." in pp.parts or name.startswith("/") or (len(name) > 1 and name[1] == ":"):
                raise BundleError(f"unsafe path in bundle: {name}")
            target = (dest / Path(*pp.parts)).resolve() if pp.parts else dest
            if target != dest and dest not in target.parents:
                raise BundleError(f"unsafe path in bundle: {name}")
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not m.isfile():
                continue  # no links, devices or fifos
            total += m.size
            if total > limit:
                raise BundleError("bundle too large")
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(m)
            with open(target, "wb") as f:
                while chunk := src.read(1 << 20):
                    f.write(chunk)
            written.append(pp.as_posix())
    return written


def chunks(data: bytes, size: int = CHUNK):
    n = max(1, -(-len(data) // size))
    for i in range(n):
        yield i, n, base64.b64encode(data[i * size:(i + 1) * size]).decode()


def join_chunks(parts: dict[int, str], n: int) -> bytes:
    if len(parts) != n:
        raise BundleError(f"missing parts: got {len(parts)} of {n}")
    return b"".join(base64.b64decode(parts[i]) for i in range(n))
