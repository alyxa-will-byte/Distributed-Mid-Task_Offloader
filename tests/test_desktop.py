import sys

import pytest
from conftest import Worker, axr, wait_for

from axr import desktop


def test_config_string_is_the_form_rustdesk_parses():
    assert desktop.config_string("rd.example.net", "abc+/=") == "host=rd.example.net,key=abc+/=,relay=rd.example.net,"
    with pytest.raises(ValueError):
        desktop.config_string("rd.example.net", "a,b")


def test_setup_script_sets_password_and_guard(tmp_path):
    s = desktop.setup_script(installer=tmp_path / "i.exe", config="host=h,key=k,relay=h,", password="pw123",
                             result=tmp_path / "r.json", guard=(tmp_path / "g.ps1", desktop.guard_script(tmp_path / "identity.json")))
    assert "--silent-install" in s and "--password 'pw123'" in s and "--get-id" in s
    assert "axr-desktop-guard" in s and "/RU SYSTEM" in s
    viewer = desktop.setup_script(installer=tmp_path / "i.exe", config="c", password=None, result=tmp_path / "r.json", guard=None)
    assert "--password" not in viewer and "schtasks" not in viewer


def test_guard_turns_desktop_off_when_unpaired(tmp_path):
    g = desktop.guard_script(tmp_path / "it's" / "identity.json")
    assert "Test-Path -LiteralPath" in g and "it''s" in g          # quoted safely
    assert "Stop-Service -Name RustDesk" in g and "--password" in g and "Disabled" in g


def test_password_is_strong():
    pw = desktop.new_password()
    assert len(pw) == 16 and pw.isalnum() and desktop.new_password() != pw


def test_store_roundtrip(tmp_path):
    st = desktop.Store(tmp_path)
    st.put("abc123", {"name": "TronaldsDump", "rustdesk_id": "123456789", "password": "x", "host": "h"})
    assert st.find("tronaldsdump")[1]["rustdesk_id"] == "123456789"
    assert st.find("abc")[0] == "abc123" and st.find("nope") is None


@pytest.mark.skipif(sys.platform == "win32", reason="on Windows this would really install RustDesk (CI does that in ci_desktop.py)")
def test_desktop_setup_reports_unsupported_helper(mesh):
    r = axr(mesh["laptop"], "desktop", "setup", "helper1", "--key", "testkey", timeout=90)
    assert r.returncode != 0 and "Windows helpers only" in r.stdout + r.stderr, r.stdout + r.stderr
    assert "No remote desktops yet" in axr(mesh["laptop"], "desktop", "list").stdout


def test_desktop_setup_refused_while_paused(mesh):
    axr(mesh["laptop"], "devices", "pause", "helper1", check=True)
    r = axr(mesh["laptop"], "desktop", "setup", "helper1", "--key", "testkey", timeout=90)
    assert r.returncode != 0 and "paused" in r.stdout + r.stderr


def test_desktop_needs_key_and_known_helper(mesh):
    r = axr(mesh["laptop"], "desktop", "setup", "helper1")
    assert r.returncode != 0 and "--key" in r.stderr
    r = axr(mesh["laptop"], "desktop", "helper1")
    assert r.returncode != 0 and "axr desktop setup helper1" in r.stderr
