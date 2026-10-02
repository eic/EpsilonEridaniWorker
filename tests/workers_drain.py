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
# A graceful restart a worker never honours is forced after this; the stand-in "rounds" take 1s.
os.environ["EPSILONERIDANI_GRACEFUL_RESTART_TIMEOUT"] = "4"
# Stands in for `work --loop`: run until a drain is requested, then take a moment to "finish the round
# in flight" before exiting 0, as the real loop does. If the manager killed it instead, the runner would
# record the stop ("stopped") rather than the clean exit ("exited").
# It logs how each run ended, beside its status file: "graceful" after finishing its round, "killed"
# when it was stopped, which tells a graceful restart from a kill.
os.environ["EPSILONERIDANI_MANAGER_TEST_COMMAND"] = shlex.join(
    [
        sys.executable,
        "-c",
        "import os, pathlib, signal, sys, time\n"
        "status = pathlib.Path(os.environ['EPSILONERIDANI_RUNTIME_STATUS'])\n"
        "def note(event):\n"
        "    with open(status.with_suffix('.events'), 'a') as out: out.write(event + '\\n')\n"
        "def killed(*_):\n"
        "    note('killed'); sys.exit(143)\n"
        "signal.signal(signal.SIGTERM, killed)\n"
        "note('start')\n"
        "while os.environ.get('STANDIN_IGNORES_DRAIN'): time.sleep(0.05)\n"  # a worker that cannot drain
        "marker = status.with_suffix('.drain')\n"
        "while not marker.exists(): time.sleep(0.05)\n"
        "time.sleep(1)\n"
        "note('graceful')\n",
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
    def start_manager():
        return subprocess.Popen(
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

    def relaunched_since(before):
        return lambda: all(
            wm.runner_status(w).get("alive") and wm.runner_status(w).get("wrapper_pid") != before[w]
            for w in ("w1", "w2")
        )

    manager = start_manager()
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
    check("resume relaunches both, the on-failure worker too", bool(wait_for(relaunched_since(first))), True)

    # Resumed while the manager is down: the drained workers must start when it comes back, the
    # on-failure one included, although its clean exit would otherwise be terminal.
    second = {w: wm.runner_status(w).get("wrapper_pid") for w in ("w1", "w2")}
    check("a second drain --wait succeeds", wm.drain_workers(config, [], every=True, wait=True, timeout=20), 0)
    wm.manager_request("shutdown", stop_workers=True)
    manager.wait(15)
    manager = None
    check("resume with the manager offline succeeds", wm.resume_workers(config, [], every=True), 0)
    check(
        "...and clears the clean exits it left",
        [wm.read_json(wm.status_path(w)).get("state") for w in ("w1", "w2")],
        ["queued", "queued"],
    )
    manager = start_manager()
    check("both start when the manager returns", bool(wait_for(relaunched_since(second))), True)

    # A worker that FAILED keeps its terminal record, and with it the back-off before a relaunch.
    failed = {"state": "failed", "exit_code": 1, "stopped_at": time.time()}
    wm.update_status(wm.status_path("off"), **failed)
    wm.resume_workers(config, ["off"], every=False)
    record = wm.read_json(wm.status_path("off"))
    check("resume leaves a failed worker's record alone", (record.get("state"), record.get("exit_code")), ("failed", 1))

    # --- graceful restarts: one worker at a time, the round in flight finished first ------------------
    def events(w):
        try:
            return wm.status_path(w).with_suffix(".events").read_text().split()
        except FileNotFoundError:
            return []

    def wrapper(w):
        return wm.runner_status(w).get("wrapper_pid")

    def restarted(w, before):
        return lambda: wm.runner_status(w).get("alive") and wrapper(w) not in (None, before)

    check("both are running again", bool(wait_for(both_alive)), True)
    before, seen = wrapper("w1"), len(events("w1"))
    other = wrapper("w2")
    wm.cmd_workers(SimpleNamespace(workers_action="restart", config=config, worker_id="w1", after_round=True))
    check("restart --after-round relaunches the worker", bool(wait_for(restarted("w1", before))), True)
    new = events("w1")[seen:]
    check("...after it finished its round, without being killed", ("graceful" in new, "killed" in new), (True, False))
    check("...and leaves its sibling alone", wrapper("w2"), other)
    check("...and clears its request", wm.drain_path("w1").exists(), False)

    # Rolling out a new definition to one worker: it takes effect after that worker's round, not mid-round.
    before, seen = wrapper("w1"), len(events("w1"))
    config.write_text(config.read_text().replace('id = "w1"\n', 'id = "w1"\n\n[workers.env]\nROLLOUT = "2"\n', 1))
    new_hash = {s.id: s for s in wm.load_worker_specs(config)}["w1"].fingerprint()
    check("a changed definition restarts the worker", bool(wait_for(restarted("w1", before))), True)
    new = events("w1")[seen:]
    check("...after its round, without being killed", ("graceful" in new, "killed" in new), (True, False))
    check("...running the new definition", wm.runner_status("w1").get("spec_hash"), new_hash)
    check("...while its sibling keeps running", wrapper("w2"), other)

    # A drained worker whose definition changes stays down until resumed.
    check("drain w2", wm.drain_workers(config, ["w2"], every=False, wait=True, timeout=20), 0)
    config.write_text(config.read_text().replace('id = "w2"\n', 'id = "w2"\n\n[workers.env]\nROLLOUT = "2"\n', 1))
    time.sleep(1.0)
    check("a drained worker is not relaunched by a definition change", wm.runner_status("w2").get("alive"), False)
    wm.resume_workers(config, ["w2"], every=False)
    check("...until resumed", bool(wait_for(lambda: wm.runner_status("w2").get("alive"))), True)

    # Disabling is still immediate: the worker is stopped, round and all.
    seen = len(events("w1"))
    wm._mutate_enabled(config, "w1", False)
    check("disable still stops a worker at once", bool(wait_for(lambda: not wm.runner_status("w1").get("alive"))), True)
    check("...killing it rather than waiting for its round", events("w1")[seen:], ["killed"])

    # A worker that cannot drain (older than drain support, or hung) must not hold a restart up for ever.
    config.write_text(
        config.read_text() + '\n[[workers]]\nid = "stubborn"\n\n[workers.env]\nSTANDIN_IGNORES_DRAIN = "1"\n'
    )
    check(
        "a worker that ignores drains starts", bool(wait_for(lambda: wm.runner_status("stubborn").get("alive"))), True
    )
    before, seen = wrapper("stubborn"), len(events("stubborn"))
    asked = time.monotonic()
    wm.request_drain("stubborn", restart=True)
    check("its graceful restart is forced after the timeout", bool(wait_for(restarted("stubborn", before), 20)), True)
    check("...not before it", time.monotonic() - asked >= 4, True)
    check("...by stopping it", "killed" in events("stubborn")[seen:], True)
    check("...and the request is cleared", wm.drain_path("stubborn").exists(), False)

    # A runner holds its slot (lock, socket) before it publishes its own status, so for a moment the
    # manager reads the previous run's record. Its stale fingerprint is not a changed definition: taking
    # it for one restarted a worker that had just started on the current definition, which made the
    # manager tests flaky. The hook widens that window to 2s, about twenty manager passes.
    wm.update_status(
        wm.status_path("racer"),
        spec_hash="from-an-earlier-generation",
        wrapper_pid=1,
        state="stopped",
        stopped_at=time.time() - 60,
    )
    config.write_text(
        config.read_text()
        + '\n[[workers]]\nid = "racer"\n\n[workers.env]\nEPSILONERIDANI_TEST_RUNNER_STARTUP_DELAY = "2"\n'
    )
    check("a slowly starting worker comes up", bool(wait_for(lambda: events("racer"))), True)
    first = wrapper("racer")
    time.sleep(2.5)  # long enough for a spurious graceful restart (1s "round") to have happened
    check("...is not restarted over its previous run's record", events("racer"), ["start"])
    check("...nor asked to restart", wm.drain_path("racer").exists(), False)
    check("...and keeps running", wrapper("racer"), first)
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
