"""axr command line.

  axr                                  guided setup (the installer opens this)
  axr join <code>                      pair this device (code from Admin > Devices, or `axr pair` on your laptop)
  axr run [options] -- <command ...>   run a command on the best free helper; output streams here, out/ comes back
  axr devices                          list paired devices (online/offline, paused, load)
  axr devices pause|resume|disconnect|remove <name>
  axr pair [--role worker]             make a one-time pairing code for another device
  axr worker start                     run the helper in this terminal (the service does this for you)
  axr worker install-service           start the helper automatically and keep it running
  axr leave                            unlink this device yourself
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import sys
import time
from pathlib import Path

from . import __version__, client, service, sysinfo
from .identity import Identity, default_home
from .pairing import check_code, pair_sync
from .transport import RelayError, Removed, Session

DEFAULT_RELAY = os.environ.get("AXR_RELAY", "wss://relay.alyxrbytes.pro")


def _ident(args) -> Identity:
    ident = Identity.load(args.home)
    if not ident or not ident.id:
        sys.exit("This device is not paired yet. Run: axr join <code>")
    return ident


def _ago(ts) -> str:
    if not ts:
        return "never"
    s = max(0, time.time() - ts / 1000)
    for n, u in ((86400, "d"), (3600, "h"), (60, "min")):
        if s >= n:
            return f"{int(s // n)} {u} ago"
    return "just now"


async def _with_session(ident: Identity, fn):
    try:
        sess = await Session.open(ident)
    except Removed:
        ident.wipe()
        sys.exit("This device was removed by its owner; its keys were deleted. Pair again with a new code.")
    except RelayError as e:
        sys.exit(f"Relay: {e}")
    except OSError as e:
        sys.exit(f"Cannot reach the relay at {ident.relay}: {e}")
    try:
        return await fn(sess)
    finally:
        await sess.close()


def cmd_join(args):
    old = Identity.load(args.home)
    if old and old.id:
        sys.exit(f"This device is already paired as '{old.name}' ({old.role}). Run `axr leave` first to pair it again.")
    relay = args.relay or DEFAULT_RELAY
    code, problems = check_code(args.code)
    if not code:
        sys.exit("That code doesn't look right: " + "; ".join(problems))
    try:
        ident = pair_sync(Path(args.home or default_home()), code, relay, name=args.name, sandbox=args.sandbox, max_jobs=args.max_jobs)
    except RelayError as e:
        sys.exit(f"Pairing failed: {e}")
    except OSError as e:
        sys.exit(f"Cannot reach the relay at {relay}: {e}")
    print(f"Paired as '{ident.name}' ({ident.role}) with {relay}. This stays paired until you remove it.")
    if ident.role == "worker":
        if args.no_service:
            print("Start the helper with: axr worker start")
        else:
            try:
                print(service.install(ident.home))
            except Exception as e:
                print(f"Could not install the background service ({e}). Start it by hand with: axr worker start")


def cmd_setup(args):
    from .setup import run
    sys.exit(run(args.home, code=args.code, relay=args.relay or DEFAULT_RELAY, name=args.name, sandbox=args.sandbox,
                 max_jobs=args.max_jobs, install_service=not args.no_service, assume_yes=args.yes))


def cmd_run(args):
    ident = _ident(args)
    cmd = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not cmd:
        sys.exit("Nothing to run. Example: axr run -- python render.py")
    cwd = Path(args.dir or os.getcwd()).resolve()
    out = Path(args.out).resolve() if args.out else cwd / "out"
    if args.auto and not sysinfo.local_busy(args.cpu_limit, args.mem_limit):
        sys.exit(client.run_local(cmd, cwd))
    env = dict(e.split("=", 1) for e in args.env or [] if "=" in e)

    async def go(sess):
        return await client.offload(sess, cmd, cwd=cwd, out_dir=out, to=args.to, os_=args.os, image=args.image,
                                    local_fallback=not args.no_local, timeout=args.timeout, env=env, no_network=args.no_network)

    if ident.role != "controller":
        sys.exit("Only a controller device (your laptop) can send jobs.")
    sys.exit(asyncio.run(_with_session(ident, go)))


def cmd_devices(args):
    ident = _ident(args)

    async def go(sess):
        if args.action == "list":
            devs = await sess.devices()
            if not devs:
                print("No devices paired yet. Make a code with: axr pair")
            for d in sorted(devs, key=lambda d: (d["role"], d["name"].lower())):
                st = d.get("status") or {}
                state = "online " if d["online"] else "offline"
                load = f"cpu {st.get('cpu', 0):.0f}% mem {st.get('mem', 0):.0f}% jobs {st.get('jobs', 0)}/{st.get('max_jobs', 1)}" if d["online"] and st else ""
                seen = "" if d["online"] else f"last seen {_ago(d.get('last_seen'))}"
                me = " (this device)" if d["id"] == ident.id else ""
                print(f"{state} {'paused ' if d['paused'] else '       '}{d['role']:<10} {d['name']:<22} {d['os'] or '?':<8} {d['id']}  {load}{seen}{me}")
            return 0
        if not args.target:
            sys.exit(f"Which device? axr devices {args.action} <name>")
        r = await sess.admin(args.action, args.target)
        print({"pause": "Paused: it stays paired but gets no jobs.", "resume": "Resumed: it takes jobs again.",
               "disconnect": "Disconnected: its connection was dropped and running jobs cancelled. It reconnects later.",
               "remove": "Removed: it was unlinked and will delete its keys. Pair it again with a new code to use it."}[args.action], f"({r['id']})")
        return 0

    if ident.role != "controller":
        sys.exit("Only a controller device can see or manage devices.")
    sys.exit(asyncio.run(_with_session(ident, go)))


def cmd_pair(args):
    ident = _ident(args)

    async def go(sess):
        r = await sess.admin("pair", role=args.role)
        print(f"One-time {r['role']} code (valid 10 minutes):\n\n    {r['code']}\n")
        print(f"On the other device:  axr join {r['code']}" + (f" --relay {ident.relay}" if ident.relay != DEFAULT_RELAY else ""))
        return 0

    sys.exit(asyncio.run(_with_session(ident, go)))


def cmd_leave(args):
    ident = Identity.load(args.home)
    if not ident or not ident.id:
        sys.exit("This device is not paired.")

    async def go():
        try:
            sess = await Session.open(ident)
            await sess.leave()
            await asyncio.sleep(0.5)
            await sess.close()
        except (Removed, RelayError, OSError):
            pass

    asyncio.run(go())
    try:
        service.uninstall(quiet=True)
    except Exception:
        pass
    ident.wipe()
    print("This device was unlinked and its keys deleted.")


def cmd_worker(args):
    if args.wcmd == "join":
        return cmd_join(args)
    if args.wcmd == "leave":
        return cmd_leave(args)
    if args.wcmd == "uninstall-service":
        return print(service.uninstall())
    ident = Identity.load(args.home)
    if not ident or not ident.id:
        # Exit 0: a service manager must not keep restarting a helper that was unlinked.
        print("This device is not paired (or was removed). Run: axr join <code>")
        return
    if ident.role != "worker":
        sys.exit("This device is paired as a controller, not a helper.")
    if args.wcmd == "install-service":
        return print(service.install(ident.home))
    handlers = [logging.FileHandler(ident.home / "worker.log")]
    if sys.stderr is not None and sys.stderr.isatty():   # services log to the file only (pythonw has no console at all)
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=handlers)
    from .worker import run, single_instance
    lock = single_instance(ident.home)
    if lock is None:
        print("The helper is already running on this machine.")
        return
    result = run(ident, max_jobs=args.max_jobs, sandbox=args.sandbox)
    if result == "removed":
        # Windows cannot delete open files, so let go of the log and lock, then clear the folder again.
        logging.shutdown()
        lock.close()
        shutil.rmtree(ident.home, ignore_errors=True)
        print("This helper was removed by its owner; its keys and files were deleted.")


def main(argv=None):
    p = argparse.ArgumentParser(prog="axr", description="Send tasks from your laptop to your other devices.")
    p.add_argument("--home", help=argparse.SUPPRESS)
    p.add_argument("--version", action="version", version=f"axr {__version__}")
    sub = p.add_subparsers(dest="cmd")

    def join_args(s):
        s.add_argument("code")
        s.add_argument("--relay", help=f"relay address (default {DEFAULT_RELAY})")
        s.add_argument("--name", help="name for this device (default: computer name)")
        s.add_argument("--sandbox", choices=["process", "docker"], default="process", help="helpers only: how jobs are isolated")
        s.add_argument("--max-jobs", type=int, help="helpers only: jobs at once")
        s.add_argument("--no-service", action="store_true", help="helpers only: do not install the background service")

    s = sub.add_parser("setup", help="guided setup: scan this machine and pair it")
    s.add_argument("--code", help="pairing code (otherwise asked for; AXR_CODE also works)")
    s.add_argument("--relay", help=f"relay address (default {DEFAULT_RELAY})")
    s.add_argument("--name", help="name for this device")
    s.add_argument("--sandbox", choices=["process", "docker"], default="process")
    s.add_argument("--max-jobs", type=int)
    s.add_argument("--no-service", action="store_true")
    s.add_argument("--yes", action="store_true", help="no questions when --code is given")
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("join", help="pair this device")
    join_args(s)
    s.set_defaults(fn=cmd_join)

    s = sub.add_parser("run", help="run a command on a helper")
    s.add_argument("--to", help="a specific helper (name or id)")
    s.add_argument("--os", choices=["windows", "linux", "mac"], help="only helpers on this OS")
    s.add_argument("--image", help="run inside this container image (helpers with Docker)")
    s.add_argument("--timeout", type=float, help="seconds before the job is stopped (default 6 h)")
    s.add_argument("--env", action="append", help="KEY=VALUE for the job (repeatable)")
    s.add_argument("--dir", help="folder to send (default: current folder)")
    s.add_argument("--out", help="where results go (default: ./out)")
    s.add_argument("--no-network", action="store_true", help="image jobs: no network inside the container")
    s.add_argument("--no-local", action="store_true", help="never fall back to running here")
    s.add_argument("--auto", action="store_true", help="only offload when this machine is busy")
    s.add_argument("--cpu-limit", type=float, default=75.0)
    s.add_argument("--mem-limit", type=float, default=85.0)
    s.add_argument("command", nargs=argparse.REMAINDER)
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("devices", help="list or manage paired devices")
    s.add_argument("action", nargs="?", default="list", choices=["list", "pause", "resume", "disconnect", "remove"])
    s.add_argument("target", nargs="?")
    s.set_defaults(fn=cmd_devices)

    s = sub.add_parser("pair", help="make a pairing code for another device")
    s.add_argument("--role", choices=["worker", "controller"], default="worker")
    s.set_defaults(fn=cmd_pair)

    s = sub.add_parser("leave", help="unlink this device")
    s.set_defaults(fn=cmd_leave)

    s = sub.add_parser("whoami", help="show this device's pairing")
    s.set_defaults(fn=lambda a: print(_ident(a).describe()))

    w = sub.add_parser("worker", help="helper commands")
    ws = w.add_subparsers(dest="wcmd", required=True)
    s = ws.add_parser("start")
    s.add_argument("--max-jobs", type=int)
    s.add_argument("--sandbox", choices=["process", "docker"])
    join_args(ws.add_parser("join"))
    ws.add_parser("install-service")
    ws.add_parser("uninstall-service")
    ws.add_parser("leave")
    w.set_defaults(fn=cmd_worker)

    argv = sys.argv[1:] if argv is None else argv
    args = p.parse_args(argv)
    if not args.cmd:   # plain `axr`: open the guided setup
        args = p.parse_args([*argv, "setup"])
    args.fn(args)


if __name__ == "__main__":
    main()
