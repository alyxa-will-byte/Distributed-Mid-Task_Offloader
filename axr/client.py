"""The laptop side: pick a free helper, send it the job, show its output live, bring out/ back."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

from . import bundle, sysinfo
from .transport import Session


def _prog(cmd: list[str]) -> str:
    base = os.path.basename(cmd[0]).lower()
    return base[:-4] if base.endswith((".exe", ".cmd", ".bat")) else base


def eligible(dev: dict, *, cmd: list[str], os_: str | None = None, image: str | None = None) -> bool:
    st = dev.get("status") or {}
    if dev.get("role") != "worker" or not dev.get("online") or dev.get("paused") or not st:
        return False
    if os_ and dev.get("os") != os_:
        return False
    if st.get("jobs", 0) >= st.get("max_jobs", 1):
        return False
    tools = set(st.get("tools") or [])
    if image:
        return "docker" in tools
    prog = _prog(cmd)
    if st.get("sandbox") == "docker":
        return "docker" in tools
    if prog in sysinfo.KNOWN_TOOLS and prog not in tools:
        return False
    return True


def score(dev: dict) -> float:
    st = dev["status"]
    return st.get("jobs", 0) / max(1, st.get("max_jobs", 1)) + st.get("cpu", 50) / 100 + st.get("mem", 50) / 200 - min(st.get("cores", 1), 64) / 256


def candidates(devs: list[dict], *, to: str | None = None, **kw) -> list[dict]:
    if to:
        hit = [d for d in devs if d["role"] == "worker" and (d["name"].lower() == to.lower() or d["id"].startswith(to))]
        if not hit:
            raise SystemExit(f"no helper called '{to}'")
        if not hit[0]["online"]:
            raise SystemExit(f"{hit[0]['name']} is offline")
        if hit[0]["paused"]:
            raise SystemExit(f"{hit[0]['name']} is paused (axr devices resume {hit[0]['name']})")
        return hit
    return sorted((d for d in devs if eligible(d, **kw)), key=score)


def run_local(cmd: list[str], cwd: Path) -> int:
    print(f"[axr] running on this machine: {' '.join(cmd)}", file=sys.stderr)
    return subprocess.call(cmd, cwd=str(cwd))


async def run_remote(sess: Session, helper: dict, cmd: list[str], *, cwd: Path, out_dir: Path, timeout: float | None = None,
                     env: dict | None = None, image: str | None = None, no_network: bool = False, quiet: bool = False,
                     stdout=None, stderr=None) -> dict:
    """Runs the job on one helper. Returns {"exit_code", "timed_out", "error", "files", "rejected"}."""
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr
    jid = uuid.uuid4().hex
    data = bundle.pack(cwd)
    parts = list(bundle.chunks(data)) if data else []
    spec = {"job": jid, "cmd": cmd, "n_chunks": len(parts), "timeout": timeout, "env": env or {}, "image": image, "no_network": no_network}
    pk, hid = helper["box_pk"], helper["id"]
    if not quiet:
        print(f"[axr] sending to {helper['name']} ({len(data) // 1024} KB): {' '.join(cmd)}", file=stderr)
    await sess.send(hid, pk, "job", spec)
    for seq, n, b in parts:
        await sess.send(hid, pk, "chunk", {"job": jid, "seq": seq, "b": b})

    async def watch_presence():
        while True:
            d = await sess.presence.get()
            if d.get("id") == hid and (d.get("removed") or not d.get("online")):
                await sess.inbox.put({"kind": "helper_gone", "data": {"job": jid}})
    watcher = asyncio.create_task(watch_presence())
    result_parts: dict[int, str] = {}
    try:
        while True:
            m = await sess.inbox.get()
            k, d = m.get("kind"), m.get("data") or {}
            if k == "closed":
                return {"exit_code": None, "error": "lost the connection to the relay"}
            if k == "undeliverable" and m.get("to") == hid:
                return {"exit_code": None, "error": m.get("error"), "rejected": True}
            if k == "helper_gone":
                return {"exit_code": None, "error": f"{helper['name']} went offline during the job"}
            if m.get("from") != hid or d.get("job") != jid:
                continue
            if k == "rejected":
                return {"exit_code": None, "error": d.get("reason"), "rejected": True}
            if k == "accepted" and not quiet:
                print(f"[axr] running on {d.get('helper')} ({d.get('sandbox')})", file=stderr)
            elif k == "out":
                (stderr if d.get("stream") == "stderr" else stdout).write(d.get("text", ""))
                (stderr if d.get("stream") == "stderr" else stdout).flush()
            elif k == "result":
                result_parts[int(d["seq"])] = d["b"]
            elif k == "done":
                files = []
                if d.get("n_result"):
                    files = bundle.safe_extract(bundle.join_chunks(result_parts, int(d["n_result"])), out_dir)
                return {"exit_code": d.get("exit_code"), "timed_out": d.get("timed_out"), "error": d.get("error"), "files": files}
    except asyncio.CancelledError:
        try:
            await asyncio.shield(sess.send(hid, pk, "cancel", {"job": jid}))
        except Exception:
            pass
        raise
    finally:
        watcher.cancel()


async def offload(sess: Session, cmd: list[str], *, cwd: Path, out_dir: Path, to=None, os_=None, image=None, local_fallback=True, **kw) -> int:
    devs = await sess.devices()
    tried = 0
    for helper in candidates(devs, to=to, cmd=cmd, os_=os_, image=image):
        r = await run_remote(sess, helper, cmd, cwd=cwd, out_dir=out_dir, image=image, **kw)
        if r.get("rejected"):
            print(f"[axr] {helper['name']}: {r.get('error')}; trying the next helper", file=sys.stderr)
            tried += 1
            continue
        if r.get("files"):
            print(f"[axr] {len(r['files'])} result file(s) in {out_dir}", file=sys.stderr)
        if r.get("timed_out"):
            print("[axr] the job ran out of time and was stopped", file=sys.stderr)
        if r.get("error"):
            print(f"[axr] {r['error']}", file=sys.stderr)
        return r["exit_code"] if isinstance(r.get("exit_code"), int) else 1
    if local_fallback and not image:
        print(f"[axr] no free helper{' (all busy)' if tried else ''}; running here instead", file=sys.stderr)
        return await asyncio.to_thread(run_local, cmd, cwd)
    print("[axr] no free helper can run this right now (axr devices shows who is online)", file=sys.stderr)
    return 1
