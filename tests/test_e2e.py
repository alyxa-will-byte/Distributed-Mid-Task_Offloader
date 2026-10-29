import time

from conftest import Worker, axr, wait_for


def test_job_runs_on_helper_and_results_come_back(mesh, project):
    r = axr(mesh["laptop"], "run", "--no-local", "--", "python", "job.py", "3", cwd=project)
    assert r.returncode == 3, r.stderr                     # exit code passes through
    assert "hello from" in r.stdout and "local" not in r.stdout
    assert "to stderr" in r.stderr and "helper1" in r.stderr
    assert (project / "out" / "result.txt").read_text() == "42626"
    work = mesh["helper"] / "work"
    wait_for(lambda: not any(work.iterdir()), what="helper workspace to be deleted")


def test_ignored_files_are_not_sent(mesh, project):
    (project / "peek.py").write_text("import os; print('GIT', os.path.exists('.git'))\n")
    r = axr(mesh["laptop"], "run", "--no-local", "--", "python", "peek.py", cwd=project)
    assert "GIT False" in r.stdout, r.stdout + r.stderr


def test_missing_tool_is_not_sent_and_falls_back(mesh, project):
    r = axr(mesh["laptop"], "run", "--no-local", "--", "definitely-not-a-tool-xyz", cwd=project)
    assert r.returncode != 0
    r = axr(mesh["laptop"], "devices")
    assert "helper1" in r.stdout and "online" in r.stdout


def test_stays_paired_across_relay_and_helper_restarts(mesh, project):
    relay, w = mesh["relay"], mesh["worker"]
    relay.stop()
    time.sleep(1)
    relay.start()                                          # registry is on disk: nobody has to pair again
    wait_for(lambda: relay.devices()["helper1"]["online"], timeout=40, what="helper to reconnect after relay restart")
    w.stop()
    wait_for(lambda: not relay.devices()["helper1"]["online"], what="helper offline")
    assert "last seen" in axr(mesh["laptop"], "devices").stdout
    w.start()
    wait_for(lambda: relay.devices()["helper1"]["online"], what="helper back online")
    assert axr(mesh["laptop"], "run", "--no-local", "--", "python", "job.py", cwd=project).returncode == 0


def test_pause_resume(mesh, project):
    laptop = mesh["laptop"]
    assert axr(laptop, "devices", "pause", "helper1").returncode == 0
    r = axr(laptop, "run", "--no-local", "--to", "helper1", "--", "python", "job.py", cwd=project)
    assert r.returncode != 0 and "paused" in r.stderr
    assert mesh["relay"].devices()["helper1"]["paused"]
    axr(laptop, "devices", "resume", "helper1", check=True)
    assert axr(laptop, "run", "--no-local", "--to", "helper1", "--", "python", "job.py", cwd=project).returncode == 0


def test_disconnect_drops_then_reconnects(mesh, project):
    relay = mesh["relay"]
    first = relay.devices()["helper1"]
    axr(mesh["laptop"], "devices", "disconnect", "helper1", check=True)
    wait_for(lambda: relay.devices()["helper1"]["online"] and mesh["worker"].proc.poll() is None, timeout=30, what="reconnect")
    assert relay.devices()["helper1"]["id"] == first["id"]  # same registration


def test_remove_while_online_wipes_helper(mesh):
    t = time.time()
    axr(mesh["laptop"], "devices", "remove", "helper1", check=True)
    mesh["worker"].proc.wait(10)
    assert time.time() - t < 5
    assert not (mesh["helper"] / "identity.json").exists()
    assert "helper1" not in mesh["relay"].devices()


def test_remove_while_offline_wipes_on_next_connect(mesh):
    w = mesh["worker"]
    w.stop()
    axr(mesh["laptop"], "devices", "remove", "helper1", check=True)
    w.start()
    w.proc.wait(20)
    assert not (mesh["helper"] / "identity.json").exists()


def test_helper_can_leave_itself(mesh):
    w = mesh["worker"]
    w.stop()
    axr(mesh["helper"], "leave", check=True)
    assert "helper1" not in mesh["relay"].devices()
    assert not (mesh["helper"] / "identity.json").exists()


def test_pairing_code_is_single_use(relay, tmp_path):
    code = relay.code("worker")
    assert axr(tmp_path / "a", "join", code, "--relay", relay.url, "--no-service").returncode == 0
    r = axr(tmp_path / "b", "join", code, "--relay", relay.url, "--no-service")
    assert r.returncode != 0 and "wrong or expired" in r.stderr


def test_workers_cannot_manage_devices(mesh):
    r = axr(mesh["helper"], "devices")
    assert r.returncode != 0


def test_two_helpers_share_work(mesh, project):
    relay = mesh["relay"]
    h2 = mesh["tmp"] / "helper2"
    axr(h2, "join", relay.code("worker"), "--relay", relay.url, "--name", "helper2", "--no-service", "--max-jobs", "1", check=True)
    w2 = Worker(h2)
    w2.start()
    try:
        wait_for(lambda: relay.devices()["helper2"]["status"], what="helper2 online")
        (project / "slow.py").write_text("import time, os; time.sleep(3); open('out/who.txt','w').write(os.environ['AXR_JOB'])\n")
        import subprocess, sys
        from conftest import ENV
        procs = [subprocess.Popen([sys.executable, "-m", "axr", "--home", str(mesh["laptop"]), "run", "--no-local", "--out", str(project / f"o{i}"), "--", "python", "slow.py"],
                                  cwd=project, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(2)]
        outs = [p.communicate(timeout=60) for p in procs]
        assert all(p.returncode == 0 for p in procs), outs
        helpers = {line.split("running on ")[1].split()[0] for _, err in outs for line in err.splitlines() if "running on" in line}
        assert helpers == {"helper1", "helper2"}, outs     # both machines did one job each
    finally:
        w2.stop()
