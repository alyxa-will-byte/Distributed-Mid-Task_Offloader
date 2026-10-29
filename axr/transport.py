"""The websocket session with the relay: pairing, signed hello, request/response, and end-to-end sealed messages."""
from __future__ import annotations

import asyncio
import itertools
import json
import platform

try:  # websockets >= 13
    from websockets.asyncio.client import connect as ws_connect
except ImportError:  # pragma: no cover - older websockets
    from websockets import connect as ws_connect  # type: ignore
from websockets.exceptions import ConnectionClosed

from .identity import Identity
from .sysinfo import os_name

MAX_MSG = 2 * 1024 * 1024


class RelayError(Exception):
    def __init__(self, msg: str, code: int | None = None):
        super().__init__(msg)
        self.code = code


class Removed(Exception):
    """The owner removed (unlinked) this device. The relay's signature on that was checked."""


def ws_url(relay: str) -> str:
    u = relay.rstrip("/")
    if u.startswith("https://"):
        u = "wss://" + u[8:]
    elif u.startswith("http://"):
        u = "ws://" + u[7:]
    elif not u.startswith(("ws://", "wss://")):
        u = "wss://" + u
    return u if u.endswith("/ws") else u + "/ws"


class Session:
    def __init__(self, ws, ident: Identity, welcome: dict):
        self.ws, self.ident, self.welcome = ws, ident, welcome
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.presence: asyncio.Queue = asyncio.Queue()
        self.paused = bool(welcome.get("paused"))
        self.removed = False
        self.close_code: int | None = None
        self._req = itertools.count(1)
        self._waiting: dict[str, asyncio.Future] = {}
        self._reader = asyncio.create_task(self._read())

    # -- connect ----------------------------------------------------------------------------
    @classmethod
    async def open(cls, ident: Identity, *, join_code: str | None = None, name: str | None = None, relay: str | None = None) -> "Session":
        url = ws_url(relay or ident.relay)
        ws = await ws_connect(url, max_size=MAX_MSG, ping_interval=20, ping_timeout=40, open_timeout=20)
        try:
            ch = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if ch.get("t") != "challenge":
                raise RelayError("the relay did not send a challenge")
            if join_code:
                await ws.send(json.dumps({"t": "join", "code": join_code.strip().lower(), "name": name or platform.node(), "os": os_name(),
                                          "arch": platform.machine().lower(), "sign_pk": ident.sign_pk, "box_pk": ident.box_pk, "sig": ident.sign(ch["nonce"])}))
            else:
                await ws.send(json.dumps({"t": "hello", "id": ident.id, "os": os_name(), "sig": ident.sign(ch["nonce"])}))
            first = None
            try:
                first = json.loads(await asyncio.wait_for(ws.recv(), 15))
            except ConnectionClosed as e:
                raise RelayError(_reason(e), _code(e)) from None
            if first.get("t") == "removed":
                if ident.relay_says_removed(first.get("id"), first.get("ts"), first.get("sig", "")):
                    raise Removed()
                raise RelayError("the relay refused this device", 4003)
            if first.get("t") == "error":
                raise RelayError(first.get("error", "refused"), 4004 if first.get("code") == "unknown" else None)
            if first.get("t") != "welcome":
                raise RelayError("unexpected answer from the relay")
            if join_code:
                ident.id, ident.role, ident.name, ident.relay_pk = first["id"], first["role"], first["name"], first["relay_pk"]
                ident.relay = relay or ident.relay
            elif ident.relay_pk and first.get("relay_pk") != ident.relay_pk:
                raise RelayError("the relay's key changed; refusing to talk to it (pair this device again if you rebuilt the relay)")
            return cls(ws, ident, first)
        except BaseException:
            await ws.close()
            raise

    # -- io ---------------------------------------------------------------------------------
    async def _read(self):
        try:
            async for raw in self.ws:
                m = json.loads(raw)
                t = m.get("t")
                if t == "msg":
                    try:
                        payload = self.ident.open(m["data"], m["from_box_pk"])
                    except Exception:
                        continue  # not sealed for us / tampered: drop
                    await self.inbox.put({"from": m["from"], "from_role": m.get("from_role"), "from_name": m.get("from_name"),
                                          "from_box_pk": m["from_box_pk"], "kind": m.get("kind"), "data": payload})
                elif t in ("devices", "admin") and str(m.get("req")) in self._waiting:
                    self._waiting.pop(str(m["req"])).set_result(m)
                elif t == "presence":
                    await self.presence.put(m["device"])
                elif t == "undeliverable":
                    await self.inbox.put({"kind": "undeliverable", "to": m.get("to"), "error": m.get("error")})
                elif t == "paused":
                    self.paused = bool(m.get("paused"))
                elif t == "removed":
                    self.removed = self.ident.relay_says_removed(m.get("id"), m.get("ts"), m.get("sig", ""))
                elif t == "error" and str(m.get("req")) in self._waiting:
                    self._waiting.pop(str(m["req"])).set_exception(RelayError(m.get("error", "error")))
        except ConnectionClosed as e:
            self.close_code = _code(e)
        finally:
            if self.close_code is None and self.ws.close_code is not None:
                self.close_code = self.ws.close_code
            for f in self._waiting.values():
                if not f.done():
                    f.set_exception(RelayError("lost the connection to the relay"))
            await self.inbox.put({"kind": "closed", "code": self.close_code, "removed": self.removed})

    async def send_raw(self, obj: dict):
        await self.ws.send(json.dumps(obj, separators=(",", ":")))

    async def send(self, to: str, peer_box_pk: str, kind: str, payload: dict):
        await self.send_raw({"t": "send", "to": to, "kind": kind, "data": self.ident.seal(payload, peer_box_pk)})

    async def request(self, obj: dict, timeout: float = 20) -> dict:
        req = str(next(self._req))
        fut = asyncio.get_running_loop().create_future()
        self._waiting[req] = fut
        await self.send_raw({**obj, "req": req})
        return await asyncio.wait_for(fut, timeout)

    async def devices(self) -> list[dict]:
        return (await self.request({"t": "devices"}))["devices"]

    async def admin(self, op: str, target: str = "", **kw) -> dict:
        r = await self.request({"t": "admin", "op": op, "target": target, **kw})
        if r.get("error"):
            raise RelayError(r["error"])
        return r

    async def status(self, status: dict):
        await self.send_raw({"t": "status", "status": status})

    async def leave(self):
        await self.send_raw({"t": "leave"})

    async def close(self):
        await self.ws.close()
        try:
            await asyncio.wait_for(self._reader, 5)
        except Exception:
            pass


def _code(e: ConnectionClosed):
    rcvd = getattr(e, "rcvd", None)
    return rcvd.code if rcvd else getattr(e, "code", None)


def _reason(e: ConnectionClosed) -> str:
    rcvd = getattr(e, "rcvd", None)
    return (rcvd.reason if rcvd else "") or "the relay closed the connection"
