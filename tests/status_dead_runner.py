#!/usr/bin/env python3
"""A worker whose runner is gone is not reported as running.

A runner that exits normally records stopped/exited/failed in its status file. One killed outright
(SIGKILL, a wiped venv) leaves whatever it last wrote, and `workers status` used to repeat it: on
bison six disabled workers read `running (desired: stopped)` for a month after an incident killed
their runners. This pins worker_snapshots for runners that are not alive (no control socket, no slot
lock):

  1. a disabled worker shows its recorded terminal state (stopped/exited/failed), else `stopped`;
  2. an enabled worker shows its recorded terminal state, else `stale` (the manager relaunches it);
  3. an enabled worker with no status file reads `stale`, as it did before (runner_status always
     returns at least `alive`, so the `missing` branch is not reached from a snapshot).

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import epsiloneridani_worker.worker_manager as wm

fails = 0


def check(name, ok, detail=""):
    global fails
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + ("" if ok else f"  {detail}"))
    fails += not ok


def main():
    state = Path(tempfile.mkdtemp(prefix="status-state-"))
    runtime = Path(tempfile.mkdtemp(prefix="status-run-"))
    os.chmod(runtime, 0o700)
    os.environ["EPSILONERIDANI_WORKERS_STATE_DIR"] = str(state)
    os.environ["EPSILONERIDANI_RUNTIME_DIR"] = str(runtime)
    recorded = {
        "off-killed-running": "running",
        "off-killed-backoff": "backoff",
        "off-stopped": "stopped",
        "off-failed": "failed",
        "on-killed-running": "running",
        "on-exited": "exited",
    }
    for wid, st in recorded.items():
        (state / f"{wid}.json").write_text(json.dumps({"id": wid, "state": st, "host": "bison"}))
    specs = [wm.WorkerSpec(id=w, enabled=w.startswith("on-")) for w in recorded] + [wm.WorkerSpec(id="on-never")]
    got = {s["id"]: s["actual"] for s in wm.worker_snapshots(specs=specs)}
    want = {
        "off-killed-running": "stopped",
        "off-killed-backoff": "stopped",
        "off-stopped": "stopped",
        "off-failed": "failed",
        "on-killed-running": "stale",
        "on-exited": "exited",
        "on-never": "stale",
    }
    for wid, w in want.items():
        check(f"{wid}: {recorded.get(wid, 'no file')} -> {w}", got.get(wid) == w, got.get(wid))
    check("none of them is alive", not any(s["alive"] for s in wm.worker_snapshots(specs=specs)))

    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
