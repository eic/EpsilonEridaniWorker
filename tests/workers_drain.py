#!/usr/bin/env python3
"""`workers drain`: a worker finishes the round in flight, stops between rounds, and stays down until
`resume`. Covers the marker and its interruptible waits, the real loop driver (no round after a drain,
the round in flight is not cut short, a long wait wakes early), and the real manager (it does not kill
a draining worker, does not relaunch a drained one, counts it healthy, and relaunches it on resume —
including an `on-failure` worker, whose clean exit is otherwise terminal)."""

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

root = Path(tempfile.mkdtemp(prefix="epsiloneridani-drain-test-"))
for key in ("EPSILONERIDANI_CONFIG_HOME", "EPSILONERIDANI_WORKERS_CONFIG", "EPSILONERIDANI_WORKERS_STATE_DIR"):
    os.environ.pop(key, None)
os.environ["XDG_CONFIG_HOME"] = str(root / "config")
os.environ["XDG_STATE_HOME"] = str(root / "state")
os.environ["EPSILONERIDANI_RUNTIME_DIR"] = str(root / "run")
# Stands in for `work --loop`: run until a drain is requested, then take a moment to "finish the round
# in flight" before exiting 0, as the real loop does. If the manager killed it instead, the runner would
# record the stop ("stopped") rather than the clean exit ("exited").
os.environ["EPSILONERIDANI_MANAGER_TEST_COMMAND"] = shlex.join(
    [
        sys.executable,
        "-c",
        "import os, pathlib, time\n"
        "marker = pathlib.Path(os.environ['EPSILONERIDANI_RUNTIME_STATUS']).with_suffix('.drain')\n"
        "while not marker.exists(): time.sleep(0.05)\n"
        "time.sleep(1)\n",
    ]
)

import epsiloneridani_worker.loop as loop  # noqa: E402
import epsiloneridani_worker.runtime_status as rs  # noqa: E402
import epsiloneridani_worker.worker_manager as wm  # noqa: E402

fails = 0


def check(name, got, expect):
    global fails
    ok = got == expect
    print(f"[{'OK ' if ok else 'XX '}] {name}: {got!r}")
    if not ok:
        print(f"      expected: {expect!r}")
        fails += 1


def wait_for(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    return None


manager = None
try:
    # --- the marker and its waits -------------------------------------------------------------------
    status = root / "unit" / "w.json"
    status.parent.mkdir(parents=True)
    os.environ.pop(rs.STATUS_ENV, None)
    check("an unmanaged worker is never drained", rs.drain_requested(), False)
    os.environ[rs.STATUS_ENV] = str(status)
    check("the marker sits beside the status file", rs.drain_marker(status), status.parent / "w.drain")
    check("no marker, no drain", rs.drain_requested(), False)
    loop.DRAIN_POLL_S = 0.05
    started = time.monotonic()
    loop._sleep(0.3)
    check("an undisturbed wait lasts as long as asked", time.monotonic() - started >= 0.3, True)
    rs.drain_marker(status).write_text("{}\n")
    started = time.monotonic()
    loop._sleep(3600)
    check("a wait wakes at once for a drain", time.monotonic() - started < 1, True)
    # Other tests drive the loop with a fake clock in place of `loop.time`; every wait must go through
    # it, and an unmanaged worker (nothing can drain it) still sleeps in one call.
    real_time, slept = loop.time, []
    loop.time = SimpleNamespace(sleep=slept.append)
    try:
        os.environ.pop(rs.STATUS_ENV)
        loop._sleep(3600)
        check("an unmanaged wait is one sleep, through loop.time", slept, [3600])
        os.environ[rs.STATUS_ENV] = str(status)
        rs.drain_marker(status).unlink()
        slept.clear()
        loop._sleep(0.12)
        check("a managed wait is sliced, through loop.time", [round(s, 2) for s in slept], [0.05, 0.05, 0.02])
    finally:
        loop.time = real_time

    # --- the loop driver ----------------------------------------------------------------------------
    rounds = []
    loop.choose_model = lambda *_args, **_kwargs: ("codex", {})
    loop.github_budget = lambda: {}
    loop.resolve_authoring_profile = lambda *_args, **_kwargs: SimpleNamespace(
        model="m", effort=None, fallback_model=None
    )
    loop.INTERROUND = 60  # a drain must cut the pause after a round short

    def one_round_then_drain(_tail):
        rounds.append(time.monotonic())
        rs.drain_marker(status).write_text("{}\n")  # requested while this round is in flight
        return 0

    args = SimpleNamespace(ignore_quota=False, bubble=False, quota_cmd=None)
    cfg = SimpleNamespace(wid="drain-test")
    rs.drain_marker(status).write_text("{}\n")
    loop.run_round_subprocess = one_round_then_drain
    check("a worker drained before it starts exits 0", loop.cmd_loop(args, cfg, only=[], agent="codex"), 0)
    check("...without running a round", len(rounds), 0)
    rs.drain_marker(status).unlink()
    started = time.monotonic()
    check("a drain during a round exits 0", loop.cmd_loop(args, cfg, only=[], agent="codex"), 0)
    check("...after letting that round finish, and no other", len(rounds), 1)
    check("...without sitting out the 60s pause", time.monotonic() - started < 10, True)
    rs.drain_marker(status).unlink()
    os.environ.pop(rs.STATUS_ENV, None)

    # --- argument checks ----------------------------------------------------------------------------
    config = root / "config" / "epsiloneridani" / "workers.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        'version = 1\n\n[[workers]]\nid = "w1"\n\n[[workers]]\nid = "w2"\nrestart = "on-failure"\n\n'
        '[[workers]]\nid = "off"\nenabled = false\n'
    )
    for label, ids, every, needle in (
        ("naming nobody is refused", [], False, "--all"),
        ("ids and --all together are refused", ["w1"], True, "not both"),
        ("an unknown id is refused", ["nope"], False, "unknown worker"),
    ):
        try:
            wm._drain_targets(config, ids, every)
            message = ""
        except wm.WorkersError as error:
            message = str(error)
        check(label, needle in message, True)
    check("--all means every enabled worker", [s.id for s in wm._drain_targets(config, [], True)], ["w1", "w2"])

    # --- the real manager ---------------------------------------------------------------------------
    manager = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "epsiloneridani_worker",
            "workers",
            "--config",
            str(config),
            "manager",
            "--interval",
            "0.1",
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    both_alive = lambda: all(wm.runner_status(w).get("alive") for w in ("w1", "w2"))  # noqa: E731
    check("the manager starts both workers", bool(wait_for(both_alive)), True)
    first = {w: wm.runner_status(w).get("wrapper_pid") for w in ("w1", "w2")}

    check(
        "drain --wait returns once both stopped",
        wm.drain_workers(config, ["w1", "w2"], every=False, wait=True, timeout=20),
        0,
    )
    for w in ("w1", "w2"):
        final = wm.read_json(wm.status_path(w))
        check(f"{w} finished its round and exited, rather than being stopped", final.get("state"), "exited")
    time.sleep(1.0)  # ten reconcile passes
    check("a drained worker is not relaunched", any(wm.runner_status(w).get("alive") for w in ("w1", "w2")), False)
    snaps = {item["id"]: item for item in wm.worker_snapshots(config=config)}
    check("status calls it drained", (snaps["w1"]["actual"], snaps["w2"]["actual"]), ("drained", "drained"))
    check("a drained fleet still reads as healthy", wm.print_worker_status(config), True)

    check("resume succeeds", wm.resume_workers(config, ["w1", "w2"], every=False), 0)
    check("resume clears the markers", any(wm.drain_path(w).exists() for w in ("w1", "w2")), False)
    relaunched = lambda: all(  # noqa: E731
        wm.runner_status(w).get("alive") and wm.runner_status(w).get("wrapper_pid") != first[w] for w in ("w1", "w2")
    )
    check("resume relaunches both, the on-failure worker too", bool(wait_for(relaunched)), True)
finally:
    if manager is not None:
        wm.manager_request("shutdown", stop_workers=True)
        try:
            manager.wait(15)
        except subprocess.TimeoutExpired:
            manager.terminate()
            manager.wait(5)
    shutil.rmtree(root, ignore_errors=True)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
