"""The helper side. Stays connected to the relay for as long as the device is on, takes jobs from your laptop, runs each
in a throwaway workspace, streams output back, returns out/ and deletes the workspace.

It keeps reconnecting forever (sleep, Wi-Fi drops, relay restarts) and only stops for good when the owner removes it."""
from __future__ import annotations

import asyncio
import logging
import os
import random
import shutil
import signal
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from . import bundle, sysinfo
from .identity import Identity
from .transport import RelayError, Removed, Session

log = logging.getLogger("axr.worker")

DEFAULT_TIMEOUT = 6 * 3600
MAX_TIMEOUT = 48 * 3600
STATUS_EVERY = 20
OUT_PIECE = 16 * 1024


@dataclass
class Job:
    id: str
    controller: str
    box_pk: str
    spec: dict
    parts: dict = field(default_factory=dict)
    task: asyncio.Task | None = None
    proc: asyncio.subprocess.Process | None = None
    docker_name: str | None = None
    cancelled: bool = False


class Worker:
    def __init__(self, ident: Identity, *, max_jobs: int | None = None, sandbox: str | None = None, image: str | None = None):
        self.ident = ident
        cfg = ident.extra.get("worker", {})
        self.max_jobs = max_jobs or cfg.get("max_jobs") or max(1, (psutil.cpu_count() or 2) // 4)
        self.sandbox = sandbox or cfg.get("sandbox", "process")       # "process" or "docker" (owner of the helper decides)
        self.image = image or cfg.get("image", "python:3.12-slim")
        self.work = ident.home / "work"
        self.jobs: dict[str, Job] = {}
        self.sess: Session | None = None
        self.stopping = False

    # -- lifecycle --------------------------------------------------------------------------
    async def run_forever(self) -> str:
        """Returns "removed" when the owner unlinked this device, "stopped" on Ctrl+C / service stop."""
        shutil.rmtree(self.work, ignore_errors=True)   # jobs never survive a restart; clear leftovers
        self.work.mkdir(parents=True, exist_ok=True)
        delay = 1.0
        while not self.stopping:
            try:
                self.sess = await Session.open(self.ident)
                log.info("connected to %s as %s", self.ident.relay, self.ident.name)
                delay = 1.0
                code = await self._serve(self.sess)
                if code == "removed":
                    return self._removed()
                if code == 4000:
                    log.warning("another copy of this device connected; waiting a minute")
                    delay = 60
                elif code == 4001:
                    log.info("the owner disconnected this device; reconnecting shortly")
                    delay = 3
            except Removed:
                return self._removed()
            except RelayError as e:
                log.warning("relay: %s", e)
                delay = 60 if e.code == 4004 else max(delay, 5)
            except (OSError, asyncio.TimeoutError) as e:
                log.info("relay not reachable (%s); retrying in %.0fs", e.__class__.__name__, delay)
            except Exception:  # never die: a helper should come back on its own
                log.exception("unexpected error; retrying")
            finally:
                await self._cancel_all()
                if self.sess:
                    await self.sess.close()
                    self.sess = None
            if self.stopping:
                break
            await asyncio.sleep(delay + random.random())
            delay = min(delay * 2, 30)
        return "stopped"

    def stop(self):
        self.stopping = True
        if self.sess:
            asyncio.ensure_future(self.sess.close())

    def _removed(self) -> str:
        log.warning("the owner removed this device: deleting its keys, cache and workspaces")
        self.ident.wipe()   # first: removing the service below can stop this very process (launchd, systemd)
        try:
            from .service import uninstall
            uninstall(quiet=True)
        except Exception:
            pass
        return "removed"

    async def _serve(self, sess: Session):
        beat = asyncio.create_task(self._status_loop(sess))
        try:
            while True:
                m = await sess.inbox.get()
                k = m.get("kind")
                if k == "closed":
                    return "removed" if m.get("removed") else m.get("code")
                if k == "undeliverable":
                    continue
                if m.get("from_role") != "controller":
                    continue   # only your controller devices may send work
                await self._handle(sess, m)
        finally:
            beat.cancel()

    async def _report(self, sess: Session):
        await sess.status(sysinfo.status(jobs=len(self.jobs), max_jobs=self.max_jobs, paused=sess.paused) | {"sandbox": self.sandbox})

    async def _status_loop(self, sess: Session):
        sysinfo.status()  # prime cpu_percent
        while True:
            try:
                await self._report(sess)
            except Exception:
                return
            await asyncio.sleep(STATUS_EVERY)

    # -- jobs -------------------------------------------------------------------------------
    async def _handle(self, sess: Session, m: dict):
        d, k, frm, pk = m["data"], m["kind"], m["from"], m["from_box_pk"]
        jid = str(d.get("job", ""))[:64]
        reply = lambda kind, payload: sess.send(frm, pk, kind, {"job": jid, **payload})  # noqa: E731
        if k == "job":
            running = len(self.jobs)   # includes jobs still receiving their files
            if sess.paused:
                return await reply("rejected", {"reason": "this helper is paused"})
            if running >= self.max_jobs or jid in self.jobs:
                return await reply("rejected", {"reason": "this helper is busy"})
            self.jobs[jid] = Job(jid, frm, pk, d)
            if int(d.get("n_chunks", 0)) == 0:
                self._start(sess, self.jobs[jid])
        elif k == "chunk":
            j = self.jobs.get(jid)
            if not j or j.controller != frm or j.task:
                return
            j.parts[int(d["seq"])] = d["b"]
            if len(j.parts) == int(j.spec.get("n_chunks", 0)):
                self._start(sess, j)
        elif k == "cancel":
            j = self.jobs.get(jid)
            if j and j.controller == frm:
                await self._cancel(j)

    def _start(self, sess: Session, j: Job):
        j.task = asyncio.create_task(self._run(sess, j))
        asyncio.ensure_future(self._report(sess))   # tell the laptop right away that this helper got busier

    async def _run(self, sess: Session, j: Job):
        ws = self.work / f"{j.id}-{uuid.uuid4().hex[:6]}"
        send = lambda kind, payload: sess.send(j.controller, j.box_pk, kind, {"job": j.id, **payload})  # noqa: E731
        result = {"exit_code": None, "timed_out": False, "error": None}
        try:
            data = bundle.join_chunks(j.parts, int(j.spec.get("n_chunks", 0))) if j.spec.get("n_chunks") else b""
            j.parts.clear()
            if data:
                bundle.safe_extract(data, ws)
            ws.mkdir(parents=True, exist_ok=True)
            (ws / "out").mkdir(exist_ok=True)
            argv, env = self._command(j, ws)
            await send("accepted", {"helper": self.ident.name, "sandbox": "docker" if j.docker_name else "process"})
            timeout = min(float(j.spec.get("timeout") or DEFAULT_TIMEOUT), MAX_TIMEOUT)
            result.update(await self._exec(j, argv, env, ws, timeout, send))
            if j.cancelled:
                result["error"] = "cancelled"
        except Exception as e:
            result["error"] = str(e) or e.__class__.__name__
        try:
            out = bundle.pack(ws, only="out") if ws.exists() and not j.cancelled else b""
            n = 0
            if out:
                for seq, n, b in bundle.chunks(out):
                    await send("result", {"seq": seq, "n": n, "b": b})
            await send("done", {**result, "n_result": n})
        except Exception as e:
            log.warning("could not send results for %s: %s", j.id, e)
        finally:
            shutil.rmtree(ws, ignore_errors=True)
            self.jobs.pop(j.id, None)
            try:
                await self._report(sess)
            except Exception:
                pass

    def _command(self, j: Job, ws: Path) -> tuple[list[str], dict]:
        cmd = [str(c) for c in j.spec.get("cmd") or []]
        if not cmd:
            raise ValueError("empty command")
        user_env = {str(k)[:100]: str(v)[:10_000] for k, v in (j.spec.get("env") or {}).items() if str(k).isidentifier()}
        if self.sandbox == "docker" or j.spec.get("image"):
            if self.sandbox != "docker" and not shutil.which("docker"):
                raise RuntimeError("this helper has no Docker for image jobs")
            j.docker_name = f"axr-{j.id[:12]}-{uuid.uuid4().hex[:6]}"
            args = ["docker", "run", "--rm", "--name", j.docker_name, "--network", "none" if j.spec.get("no_network") else "bridge",
                    "--memory", str(j.spec.get("memory") or "4g"), "--cpus", str(j.spec.get("cpus") or max(1, (psutil.cpu_count() or 2) // 2)),
                    "--pids-limit", "512", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "-v", f"{ws}:/work", "-w", "/work", "-e", "HOME=/tmp"]
            if sys.platform != "win32":
                args += ["--user", f"{os.getuid()}:{os.getgid()}"]
            for k, v in user_env.items():
                args += ["-e", f"{k}={v}"]
            return args + [str(j.spec.get("image") or self.image), *cmd], dict(os.environ)
        env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "SystemRoot", "COMSPEC", "PATHEXT", "LANG", "LC_ALL", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "WINDIR") if k in os.environ}
        tmp = ws / ".tmp"
        tmp.mkdir(exist_ok=True)
        env.update({"HOME": str(ws), "USERPROFILE": str(ws), "TMP": str(tmp), "TEMP": str(tmp), "TMPDIR": str(tmp), "AXR_JOB": j.id, "PYTHONUNBUFFERED": "1"})
        env.update(user_env)
        prog, rest = cmd[0], cmd[1:]
        if prog in ("python", "python3", "py"):
            uv = shutil.which("uv")
            reqs = next((r for r in ("requirements.txt",) if (ws / r).is_file()), None)
            if uv and reqs:
                env["UV_CACHE_DIR"] = str(self.ident.home / "uv-cache")   # shared cache, so repeat jobs start fast
                return [uv, "run", "--no-project", "--with-requirements", reqs, "python", *rest], env
            return [sys.executable, *rest], env
        local = ws / prog
        if ("/" in prog or "\\" in prog) and local.is_file():
            if sys.platform != "win32":
                local.chmod(local.stat().st_mode | 0o100)
            return [str(local), *rest], env
        found = shutil.which(prog)
        if not found:
            raise FileNotFoundError(f"'{prog}' is not installed on {self.ident.name}")
        return [found, *rest], env

    async def _exec(self, j: Job, argv, env, cwd: Path, timeout: float, send) -> dict:
        kw = {"start_new_session": True} if sys.platform != "win32" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        j.proc = await asyncio.create_subprocess_exec(*argv, cwd=str(cwd), env=env, stdin=asyncio.subprocess.DEVNULL,
                                                      stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kw)

        async def pump(stream, name):
            while piece := await stream.read(OUT_PIECE):
                await send("out", {"stream": name, "text": piece.decode("utf-8", "replace")})

        pumps = asyncio.gather(pump(j.proc.stdout, "stdout"), pump(j.proc.stderr, "stderr"))
        timed_out = False
        try:
            await asyncio.wait_for(j.proc.wait(), timeout)
        except asyncio.TimeoutError:
            timed_out = True
            await self._kill(j)
        try:
            await asyncio.wait_for(pumps, 10)
        except asyncio.TimeoutError:
            pumps.cancel()
        return {"exit_code": j.proc.returncode, "timed_out": timed_out}

    async def _kill(self, j: Job):
        if j.docker_name:
            await (await asyncio.create_subprocess_exec("docker", "kill", j.docker_name, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)).wait()
        if j.proc and j.proc.returncode is None:
            try:
                p = psutil.Process(j.proc.pid)
                for c in p.children(recursive=True):
                    c.kill()
                p.kill()
            except psutil.NoSuchProcess:
                pass

    async def _cancel(self, j: Job):
        j.cancelled = True
        if j.task:
            await self._kill(j)
        else:
            self.jobs.pop(j.id, None)

    async def _cancel_all(self):
        for j in list(self.jobs.values()):
            await self._cancel(j)
        for j in list(self.jobs.values()):
            if j.task:
                try:
                    await asyncio.wait_for(j.task, 10)
                except Exception:
                    pass
        self.jobs.clear()


def single_instance(home: Path):
    """Holds a lock while this helper runs, so two copies never fight over the same identity. None if already held."""
    f = open(home / "worker.lock", "a+")
    try:
        if sys.platform == "win32":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def run(ident: Identity, **kw) -> str:
    w = Worker(ident, **kw)

    async def main():
        loop = asyncio.get_running_loop()
        for s in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(s, w.stop)
            except (NotImplementedError, RuntimeError):
                pass  # Windows: Ctrl+C raises KeyboardInterrupt instead
        return await w.run_forever()

    try:
        return asyncio.run(main())
    except KeyboardInterrupt:
        return "stopped"

