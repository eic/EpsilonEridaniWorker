#!/usr/bin/env python3
"""`workers` from another node that shares the state directory (a cluster's shared home).

The control sockets and runner locks are per host ($XDG_RUNTIME_DIR); the status files are shared.
Before, a fleet running on one node read as "manager: offline" with every worker dead when viewed from
another, and a command there could start a second manager. Properties under test:

  - a fresh manager heartbeat from another host reads as "running on <host>", not "offline";
  - a stale, stopped, or same-host heartbeat does not count as a remote manager;
  - a worker heartbeating from another host counts as alive there, and says so;
  - a stale worker record from another host does not;
  - the fleet reads as healthy when its manager runs elsewhere;
  - ensure_manager() defers to a remote manager instead of spawning one here;
  - run_manager() refuses to start while another host's manager is heartbeating;
  - the heartbeat file is not mistaken for a worker (it is not *.json).

Stdlib only; "another host" is a status record stamped with a different hostname.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

root = Path(tempfile.mkdtemp(prefix="epsiloneridani-remote-host-test-"))
for key in ("EPSILONERIDANI_CONFIG_HOME", "EPSILONERIDANI_WORKERS_CONFIG", "EPSILONERIDANI_WORKERS_STATE_DIR"):
    os.environ.pop(key, None)
os.environ["XDG_CONFIG_HOME"] = str(root / "config")
os.environ["XDG_STATE_HOME"] = str(root / "state")
os.environ["EPSILONERIDANI_RUNTIME_DIR"] = str(root / "run")  # empty: nothing runs on "this" host

import epsiloneridani_worker.worker_manager as wm  # noqa: E402

fails = 0


def check(name, got, expect):
    global fails
    ok = got == expect
    print(f"[{'OK ' if ok else 'XX '}] {name}: {got!r}")
    if not ok:
        print(f"      expected: {expect!r}")
        fails += 1


OTHER = "bison-" + wm.this_host()  # any name that is not this host
state = wm.workers_state_dir()
state.mkdir(parents=True, exist_ok=True)
config = root / "config" / "epsiloneridani" / "workers.toml"
config.parent.mkdir(parents=True)
config.write_text('version = 1\n\n[[workers]]\nid = "w1"\n\n[[workers]]\nid = "w2"\n')


def heartbeat(**fields):
    record = {"host": OTHER, "pid": 4242, "heartbeat_at": time.time(), "stopped_at": None, **fields}
    wm.manager_heartbeat_path().write_text(json.dumps(record))


FINGERPRINT = {spec.id: spec.fingerprint() for spec in wm.load_worker_specs(config)}


def worker_record(wid, **fields):
    record = {
        "id": wid,
        "spec_hash": FINGERPRINT[wid],  # as the runner on the other host recorded it
        "host": OTHER,
        "state": "waiting-quota",
        "alive": True,
        "heartbeat_at": time.time(),
        "stopped_at": None,
        "detail": "claude ~ (weekly ahead of pace)",
        **fields,
    }
    wm.status_path(wid).write_text(json.dumps(record))


# --- the manager's heartbeat ----------------------------------------------------------------------
check("no heartbeat: no remote manager", wm.remote_manager(), None)
heartbeat()
check("fresh heartbeat from another host: remote manager", (wm.remote_manager() or {}).get("host"), OTHER)
heartbeat(heartbeat_at=time.time() - 120)
check("stale heartbeat: not running", wm.remote_manager(), None)
heartbeat(stopped_at=time.time())
check("stopped manager: not running", wm.remote_manager(), None)
heartbeat(host=wm.this_host())
check("same host: the local socket is the authority, not the file", wm.remote_manager(), None)

heartbeat()
line = wm._manager_line(False, wm.remote_manager())
check("status line names the host", line.startswith(f"manager: running on {OTHER} (heartbeat "), True)
check("status line says control is there", "live details and control only there" in line, True)
check("no remote: offline", wm._manager_line(False, None), "manager: offline")
check("local ping wins", wm._manager_line(True, wm.remote_manager()), "manager: running")

# --- workers --------------------------------------------------------------------------------------
worker_record("w1")
worker_record("w2", heartbeat_at=time.time() - 300)
snaps = {s["id"]: s for s in wm.worker_snapshots(config=config)}
check("heartbeat file is not a worker", sorted(snaps), ["w1", "w2"])
check("remote worker is alive", snaps["w1"]["alive"], True)
check("remote worker names its host", snaps["w1"]["remote_host"], OTHER)
check("remote worker keeps its reported state", snaps["w1"]["actual"], "waiting-quota")
check("stale remote worker is not alive", snaps["w2"]["alive"], False)
check("stale remote worker has no live host", snaps["w2"]["remote_host"], None)
worker_record("w2", spec_hash="an-older-generation")
snaps_old = {s["id"]: s for s in wm.worker_snapshots(config=config)}
check("remote worker on an older config reads as restarting", snaps_old["w2"]["actual"], "restarting")

lines = wm._worker_status_lines(config, list(snaps.values()), False, width=100, remote=wm.remote_manager())
check("rendered manager line", lines[0].startswith(f"manager: running on {OTHER}"), True)
check("rendered worker heading", f"w1 — waiting for quota (on {OTHER})" in lines, True)

worker_record("w2")
devnull = open(os.devnull, "w")
saved, sys.stdout = sys.stdout, devnull
try:
    healthy = wm.print_worker_status(config)
finally:
    sys.stdout = saved
check("fleet running elsewhere reads healthy", healthy, True)

# --- no second manager ----------------------------------------------------------------------------
spawned = []
real_popen = subprocess.Popen
subprocess.Popen = lambda *a, **k: spawned.append(a) or (_ for _ in ()).throw(AssertionError("spawned"))
try:
    check("ensure_manager defers to the remote manager", wm.ensure_manager(config), False)
finally:
    subprocess.Popen = real_popen
check("ensure_manager spawned nothing", spawned, [])

try:
    wm.run_manager(config, interval=0.1)
    refused = False
except SystemExit as exc:
    refused = exc.code == 2
check("run_manager refuses while another host's manager is alive", refused, True)
check("the refusal left the other host's heartbeat alone", (wm.remote_manager() or {}).get("host"), OTHER)

print()
if fails:
    print(f"FAIL: {fails} mismatch(es)")
    sys.exit(1)
print("all remote-host checks passed")
