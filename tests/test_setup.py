import re
import subprocess
import sys
from pathlib import Path

from conftest import ENV, ROOT, axr

from axr.pairing import WORDS, check_code, fix_code


def flat(text: str) -> str:
    """The screen's text with line wrapping and panel borders removed, so checks don't depend on terminal width."""
    return " ".join(text.replace("│", " ").split())


def test_code_words_match_the_relay():
    src = (ROOT / "relay" / "relay.mjs").read_text()
    m = re.search(r"const WORDS = \((.*?)\)\.split", src, re.S)
    relay_words = "".join(re.findall(r"'([^']*)'", m.group(1))).split()
    assert relay_words == WORDS


def test_check_code_accepts_spaces_and_case():
    assert check_code("Amber oak  River-sage tide_wren")[0] == "amber-oak-river-sage-tide-wren"


def test_check_code_explains_problems():
    code, problems = check_code("amber oak rivr sage tide")
    assert code is None and any("did you mean 'river'" in p for p in problems)
    assert check_code("amber oak river sage tide")[1] == ["a code has 6 words, this has 5"]


def test_fix_code_only_fixes_clear_typos():
    assert fix_code("amber oak rivr sage tide wren") == "amber-oak-river-sage-tide-wren"
    assert fix_code("amber oak zzzz sage tide wren") is None


def test_setup_screen_pairs_and_fixes_a_typo(relay, tmp_path):
    code = relay.code("worker")
    words = code.split("-")
    typo = " ".join([words[0] + "x", *words[1:]])
    r = subprocess.run([sys.executable, "-m", "axr", "--home", str(tmp_path / "h"), "setup", "--relay", relay.url, "--no-service", "--name", "box1"],
                       input=f"1\n{typo}\ny\n", env={**ENV, "AXR_NO_ANIM": "1"}, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "did you mean" in flat(r.stdout) and "box1 is now a helper" in flat(r.stdout), r.stdout
    assert (tmp_path / "h" / "identity.json").exists()
    # Opening it again shows the paired menu instead of asking for a code.
    r = subprocess.run([sys.executable, "-m", "axr", "--home", str(tmp_path / "h")], input="q\n", env={**ENV, "AXR_NO_ANIM": "1"},
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert "already paired" in flat(r.stdout), r.stdout


def test_setup_unattended_with_code(relay, tmp_path):
    r = axr(tmp_path / "h", "setup", "--code", relay.code("controller"), "--yes", "--relay", relay.url, "--name", "lap1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "lap1 is your controller" in flat(r.stdout), r.stdout


def test_setup_unattended_bad_code_fails_cleanly(relay, tmp_path):
    r = axr(tmp_path / "h", "setup", "--code", "amber-oak-river-sage-tide-wren", "--yes", "--relay", relay.url)
    assert r.returncode != 0 and "wrong or expired" in flat(r.stdout), r.stdout
    assert not (Path(tmp_path / "h") / "identity.json").exists()


def test_only_one_helper_runs_per_device(mesh):
    r = axr(mesh["helper"], "worker", "start", timeout=20)
    assert r.returncode == 0 and "already running" in r.stdout
