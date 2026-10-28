import io
import tarfile

import pytest

from axr import bundle, client
from axr.identity import Identity


def _tar(name, data=b"x"):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        ti = tarfile.TarInfo(name)
        ti.size = len(data)
        t.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


@pytest.mark.parametrize("name", ["../evil.txt", "/etc/evil", "a/../../evil", "C:/evil"])
def test_safe_extract_rejects_escapes(tmp_path, name):
    with pytest.raises(bundle.BundleError):
        bundle.safe_extract(_tar(name), tmp_path / "d")


def test_safe_extract_skips_links(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        ti = tarfile.TarInfo("link")
        ti.type = tarfile.SYMTYPE
        ti.linkname = "/etc/passwd"
        t.addfile(ti)
    assert bundle.safe_extract(buf.getvalue(), tmp_path / "d") == []


def test_pack_roundtrip_and_ignore(tmp_path):
    (tmp_path / "a.txt").write_text("hi")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("big")
    (tmp_path / "secret.key").write_text("k")
    (tmp_path / ".axrignore").write_text("*.key\n")
    files = bundle.safe_extract(bundle.pack(tmp_path), tmp_path / "x")
    assert "a.txt" in files and not any("node_modules" in f or f.endswith(".key") for f in files)


def test_chunks_roundtrip():
    data = bytes(range(256)) * 5000
    parts = {i: b for i, n, b in bundle.chunks(data, 1000)}
    assert bundle.join_chunks(parts, len(parts)) == data


def test_seal_is_end_to_end(tmp_path):
    a, b, c = (Identity.fresh(tmp_path) for _ in range(3))
    sealed = a.seal({"x": 1}, b.box_pk)
    assert b.open(sealed, a.box_pk) == {"x": 1}
    with pytest.raises(Exception):
        c.open(sealed, a.box_pk)       # anyone else (the relay included) cannot read it


def _dev(name, **st):
    return {"id": name, "name": name, "role": "worker", "online": True, "paused": False, "os": "linux", "box_pk": "",
            "status": {"cpu": 10, "mem": 10, "cores": 8, "jobs": 0, "max_jobs": 1, "tools": ["python", "ffmpeg"], **st}}


def test_scheduler_prefers_idle_and_matching():
    devs = [_dev("busy", cpu=95), _dev("idle", cpu=5), _dev("full", jobs=1), {**_dev("paused"), "paused": True}, _dev("noff", tools=["python"])]
    names = [d["name"] for d in client.candidates(devs, cmd=["ffmpeg", "-i", "x"])]
    assert names == ["idle", "busy"]
    assert [d["name"] for d in client.candidates(devs, cmd=["python", "x.py"])][0] == "idle"
    assert client.candidates(devs, cmd=["python"], os_="windows") == []
