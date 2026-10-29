"""`axr setup`: the guided screen the installer opens. Banner, machine scan, pairing code (with typo help), live steps."""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

import psutil
from rich.align import Align
from rich.console import Console, Group
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table
from rich.text import Text

from . import __version__, service, sysinfo
from .identity import Identity, default_home
from .pairing import check_code, fix_code, pair_sync
from .transport import RelayError

LOGO = r"""
   █████╗ ██╗  ██╗██████╗
  ██╔══██╗╚██╗██╔╝██╔══██╗
  ███████║ ╚███╔╝ ██████╔╝
  ██╔══██║ ██╔██╗ ██╔══██╗
  ██║  ██║██╔╝ ██╗██║  ██║
  ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝
"""
GRADIENT = ["#ff4fd8", "#d65cff", "#a66bff", "#7a7cff", "#4f95ff", "#2fb7ff", "#22d3ee"]
ACCENT = "#a66bff"


def _animate(console: Console) -> bool:
    return console.is_terminal and not os.environ.get("AXR_NO_ANIM")


def banner(console: Console):
    lines = [ln for ln in LOGO.splitlines() if ln.strip()]
    anim = _animate(console)
    for i, ln in enumerate(lines):
        console.print(Align.center(Text(ln, style=f"bold {GRADIENT[i % len(GRADIENT)]}")))
        if anim:
            time.sleep(0.05)
    console.print(Align.center(Text.assemble(("distributed task offloader", "bold white"), ("  ·  ", "dim"), ("alyxrbytes.pro", ACCENT), ("  ·  ", "dim"), (f"v{__version__}", "dim"))))
    console.print()


def _gpu() -> str:
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"], capture_output=True, text=True, timeout=5).stdout.strip()
            if out:
                return out.splitlines()[0].replace(", ", " · ")
        except Exception:
            pass
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return "Apple Silicon (integrated)"
    return "none detected"


def scan(console: Console) -> dict:
    with console.status(f"[{ACCENT}]Scanning this machine…", spinner="dots12"):
        info = {"name": platform.node() or "this-machine", "os": f"{sysinfo.os_name()} · {platform.release()} · {platform.machine().lower()}",
                "cpu": f"{psutil.cpu_count(logical=False) or '?'} cores / {psutil.cpu_count() or '?'} threads",
                "ram": f"{psutil.virtual_memory().total / 2**30:.1f} GB", "gpu": _gpu(), "tools": sysinfo.tools()}
        if _animate(console):
            time.sleep(0.6)
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", justify="right")
    t.add_column()
    t.add_row("device", f"[bold]{info['name']}[/]")
    t.add_row("system", info["os"])
    t.add_row("cpu", info["cpu"])
    t.add_row("memory", info["ram"])
    t.add_row("gpu", info["gpu"])
    shown = [x for x in ("python", "node", "ffmpeg", "blender", "docker", "uv", "nvidia-smi") if x != "python3"]
    t.add_row("tools", "  ".join(f"[green]✓ {x}[/]" if x in info["tools"] else f"[dim]✗ {x}[/]" for x in shown))
    console.print(Panel(t, title="[bold]this machine[/]", border_style=ACCENT, padding=(1, 2)))
    return info


def _step(console: Console, label: str, fn):
    """Runs fn with a spinner; prints a tick or a cross. Returns fn's result or raises."""
    with console.status(f"[{ACCENT}]{label}…", spinner="dots12"):
        try:
            r = fn()
        except Exception:
            console.print(f"  [red]✗[/] {label}")
            raise
    console.print(f"  [green]✓[/] {label}")
    return r


def ask_code(console: Console, preset: str | None = None) -> str | None:
    hint = Text.assemble(("Get a code from ", "dim"), ("Admin › Devices", ACCENT), (" or by running ", "dim"), ("axr pair", ACCENT),
                         (" on your laptop.\nSix words, like ", "dim"), ("amber-oak-river-sage-tide-wren", "bold"), (" (spaces are fine).", "dim"))
    console.print(Panel(hint, title="[bold]pairing code[/]", border_style=ACCENT, padding=(0, 2)))
    for attempt in range(6):
        raw = preset if (preset and attempt == 0) else Prompt.ask(f"[bold {ACCENT}]›[/] code", console=console)
        if raw.strip().lower() in ("q", "quit", "exit"):
            return None
        code, problems = check_code(raw)
        if code:
            return code
        for p in problems:
            console.print(f"  [yellow]![/] {p}")
        guess = fix_code(raw)
        if guess and Confirm.ask(f"  Use [bold]{guess}[/]?", default=True, console=console):
            return guess
    return None


def advanced(console: Console, opts: dict) -> dict:
    console.print(Panel("[dim]Press Enter to keep each value.[/]", title="[bold]advanced[/]", border_style=ACCENT))
    opts["name"] = Prompt.ask("device name", default=opts["name"], console=console)
    opts["relay"] = Prompt.ask("relay address", default=opts["relay"], console=console)
    if Confirm.ask("is this a helper you want to isolate jobs on with Docker?", default=opts["sandbox"] == "docker", console=console):
        opts["sandbox"] = "docker"
    opts["max_jobs"] = IntPrompt.ask("jobs at once (helpers)", default=opts["max_jobs"] or max(1, (psutil.cpu_count() or 2) // 4), console=console)
    return opts


def done_panel(console: Console, ident: Identity, service_msg: str | None):
    if ident.role == "worker":
        body = Group(
            Text.assemble(("✓ ", "bold green"), (ident.name, "bold"), (" is now a helper", "bold green")),
            Text(""),
            Text("It takes jobs from your laptop whenever it is on, and reconnects on its own after restarts, sleep or Wi-Fi drops.", style="white"),
            Text(service_msg or "Start it with: axr worker start", style="dim"),
            Text(""),
            Text.assemble(("Manage it from your laptop:  ", "dim"), ("axr devices", ACCENT), ("  ·  pause / disconnect / remove", "dim")))
    else:
        body = Group(
            Text.assemble(("✓ ", "bold green"), (ident.name, "bold"), (" is your controller", "bold green")),
            Text(""),
            Text.assemble(("Send work out:   ", "dim"), ("axr run -- python job.py", ACCENT)),
            Text.assemble(("Add a helper:    ", "dim"), ("axr pair", ACCENT), ("  then run the installer on that machine", "dim")),
            Text.assemble(("See devices:     ", "dim"), ("axr devices", ACCENT)))
    console.print()
    console.print(Panel(body, border_style="green", padding=(1, 2), title="[bold green]paired[/]"))


def paired_menu(console: Console, ident: Identity) -> int:
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", justify="right")
    t.add_column()
    t.add_row("paired as", f"[bold]{ident.name}[/] ({'helper' if ident.role == 'worker' else 'controller'})")
    t.add_row("relay", ident.relay)
    t.add_row("device id", ident.id)
    console.print(Panel(t, title="[bold green]already paired[/]", border_style="green", padding=(1, 2)))
    choices = {"1": "check the connection"}
    if ident.role == "worker":
        choices["2"] = "(re)install the background service"
    choices["3"] = "unlink this device"
    choices["q"] = "quit"
    for k, v in choices.items():
        console.print(f"  [bold {ACCENT}]{k}[/]  {v}")
    pick = Prompt.ask(f"[bold {ACCENT}]›[/]", choices=list(choices), default="q", console=console)
    if pick == "1":
        import asyncio

        from .transport import Session

        async def ping():
            s = await Session.open(ident)
            await s.close()
        try:
            _step(console, f"Connecting to {ident.relay}", lambda: asyncio.run(ping()))
            console.print("  [green]The relay knows this device and it is reachable.[/]")
        except Exception as e:
            console.print(f"  [red]{e}[/]")
            return 1
    elif pick == "2":
        console.print(f"  [green]✓[/] {_step(console, 'Installing the background service', lambda: service.install(ident.home))}")
    elif pick == "3" and Confirm.ask("  Unlink this device and delete its keys?", default=False, console=console):
        from .cli import cmd_leave

        class A:
            home = str(ident.home)
        cmd_leave(A)
    return 0


def utf8_console():
    """The screen uses box and block characters. Old Windows consoles default to a code page that has none of them."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        if enc != "utf8" and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def run(home: str | None = None, *, code: str | None = None, relay: str, name: str | None = None, sandbox: str = "process",
        max_jobs: int | None = None, install_service: bool = True, assume_yes: bool = False) -> int:
    utf8_console()
    console = Console()
    console.print()
    banner(console)
    home_p = Path(home or default_home())
    existing = Identity.load(home_p)
    if existing and existing.id:
        return paired_menu(console, existing)
    info = scan(console)
    opts = {"name": name or info["name"], "relay": relay, "sandbox": sandbox, "max_jobs": max_jobs}
    code = code or os.environ.get("AXR_CODE")
    if not code:   # a code from the installer (AXR_CODE / --code) goes straight to pairing
        console.print(f"  [bold {ACCENT}]1[/]  pair this machine with a code   [dim](recommended)[/]")
        console.print(f"  [bold {ACCENT}]2[/]  advanced settings first          [dim]name, relay, Docker, jobs at once[/]")
        console.print(f"  [bold {ACCENT}]q[/]  quit")
        pick = Prompt.ask(f"[bold {ACCENT}]›[/]", choices=["1", "2", "q"], default="1", console=console)
        if pick == "q":
            return 0
        if pick == "2":
            opts = advanced(console, opts)
    for _ in range(3):
        if code and assume_yes:   # unattended: no questions; a bad code just fails
            code, problems = check_code(code)
            if not code:
                console.print("  [red]" + "; ".join(problems) + "[/]")
                return 1
        else:
            code = ask_code(console, preset=code)
        if not code:
            console.print("[dim]Nothing changed. Run [bold]axr setup[/] whenever you have a code.[/]")
            return 1
        console.print()
        try:
            ident = _step(console, f"Creating this device's keys and pairing with {opts['relay'].split('://')[-1]}",
                          lambda: pair_sync(home_p, code, opts["relay"], name=opts["name"], sandbox=opts["sandbox"], max_jobs=opts["max_jobs"]))
            break
        except RelayError as e:
            console.print(f"  [red]{e}[/]")
            code = None
            if "expired" not in str(e) and "wrong" not in str(e):
                return 1
            console.print("  [dim]Make a fresh code and try again.[/]\n")
        except OSError as e:
            console.print(f"  [red]Cannot reach the relay ({e}). Check the internet connection and the relay address.[/]")
            return 1
    else:
        return 1
    where = "in the OS credential store" if json.loads((ident.home / "identity.json").read_text()).get("secrets") == "keyring" else "in a file only you can read"
    console.print(f"  [green]✓[/] Saved this device's keys {where}")
    svc = None
    if ident.role == "worker" and install_service:
        try:
            svc = _step(console, "Installing the background service", lambda: service.install(ident.home))
        except Exception as e:
            svc = f"Could not install the background service ({e}). Start the helper with: axr worker start"
    done_panel(console, ident, svc)
    return 0
