"""A device's permanent identity: its keys, its id at the relay, and the relay's pinned public key.

Created once by `axr join <code>` and kept until you remove the device. Nothing in here expires.
Secret keys go into the OS credential store (Windows Credential Manager / macOS Keychain / Secret Service)
when `keyring` is installed and works; otherwise into identity.json, readable only by this user.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from nacl.public import Box, PrivateKey, PublicKey
from nacl.signing import SigningKey, VerifyKey

KEYRING_SERVICE = "axr-offload"


def default_home() -> Path:
    if env := os.environ.get("AXR_HOME"):
        return Path(env)
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "axr"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "axr"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "axr"


b64e = lambda b: base64.b64encode(bytes(b)).decode()  # noqa: E731
b64d = lambda s: base64.b64decode(s)  # noqa: E731


def _keyring():
    if os.environ.get("AXR_NO_KEYRING"):
        return None
    try:
        import keyring  # optional dependency

        keyring.get_password(KEYRING_SERVICE, "probe")
        return keyring
    except Exception:
        return None


@dataclass
class Identity:
    home: Path
    sign_sk: SigningKey
    box_sk: PrivateKey
    relay: str = ""
    id: str = ""
    role: str = ""
    name: str = ""
    relay_pk: str = ""
    extra: dict = field(default_factory=dict)

    # -- keys -------------------------------------------------------------------------------
    @classmethod
    def fresh(cls, home: Path) -> "Identity":
        return cls(home=home, sign_sk=SigningKey.generate(), box_sk=PrivateKey.generate())

    @property
    def sign_pk(self) -> str:
        return b64e(self.sign_sk.verify_key)

    @property
    def box_pk(self) -> str:
        return b64e(self.box_sk.public_key)

    def sign(self, msg: str | bytes) -> str:
        data = msg.encode() if isinstance(msg, str) else msg
        return b64e(self.sign_sk.sign(data).signature)

    def seal(self, obj: dict, peer_box_pk: str) -> str:
        """Encrypt and authenticate a message for one peer device (the relay cannot read it)."""
        box = Box(self.box_sk, PublicKey(b64d(peer_box_pk)))
        return b64e(box.encrypt(json.dumps(obj, separators=(",", ":")).encode()))

    def open(self, data: str, peer_box_pk: str) -> dict:
        box = Box(self.box_sk, PublicKey(b64d(peer_box_pk)))
        return json.loads(box.decrypt(b64d(data)))

    def relay_says_removed(self, id_: str, ts, sig: str) -> bool:
        """True only if the pinned relay key really signed 'this device was removed'."""
        if not self.relay_pk or id_ != self.id:
            return False
        try:
            VerifyKey(b64d(self.relay_pk)).verify(f"axr-removed:{id_}:{ts}".encode(), b64d(sig))
            return True
        except Exception:
            return False

    # -- storage ----------------------------------------------------------------------------
    @property
    def file(self) -> Path:
        return self.home / "identity.json"

    def save(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        secrets = {"sign_sk": b64e(self.sign_sk), "box_sk": b64e(self.box_sk)}
        kr = _keyring()
        meta = {"relay": self.relay, "id": self.id, "role": self.role, "name": self.name, "relay_pk": self.relay_pk, "extra": self.extra}
        if kr and self.id:
            kr.set_password(KEYRING_SERVICE, self.id, json.dumps(secrets))
            meta["secrets"] = "keyring"
        else:
            meta["secrets"] = secrets
        tmp = self.file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(meta, f, indent=1)
        os.replace(tmp, self.file)

    @classmethod
    def load(cls, home: Path | None = None) -> "Identity | None":
        home = Path(home) if home else default_home()
        try:
            meta = json.loads((home / "identity.json").read_text())
        except (OSError, ValueError):
            return None
        secrets = meta.get("secrets")
        if secrets == "keyring":
            kr = _keyring()
            raw = kr.get_password(KEYRING_SERVICE, meta["id"]) if kr else None
            if not raw:
                raise RuntimeError("this device's keys are in the OS credential store but could not be read")
            secrets = json.loads(raw)
        return cls(home=home, sign_sk=SigningKey(b64d(secrets["sign_sk"])), box_sk=PrivateKey(b64d(secrets["box_sk"])),
                   relay=meta.get("relay", ""), id=meta.get("id", ""), role=meta.get("role", ""), name=meta.get("name", ""),
                   relay_pk=meta.get("relay_pk", ""), extra=meta.get("extra") or {})

    def wipe(self) -> None:
        """Forget everything: keys, cache, job folders. Used when the owner removes this device."""
        kr = _keyring()
        if kr and self.id:
            try:
                kr.delete_password(KEYRING_SERVICE, self.id)
            except Exception:
                pass
        shutil.rmtree(self.home, ignore_errors=True)

    def describe(self) -> dict:
        d = asdict(self)
        for k in ("sign_sk", "box_sk", "home"):
            d.pop(k, None)
        return d
