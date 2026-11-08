#!/usr/bin/env python3
"""Worker ids the worker hands out are zero-padded: worker01 ... worker99, then worker100.

An operator renamed their workers worker1-9 to worker01-09 so ids sort the way they count (state
directories, logs, `workers status`), and then found `epsiloneridani work` without --worker-id still
creating a state/worker1 beside them. This pins both places ids are chosen:

  1. worker_id(n) pads to two digits and grows past 99;
  2. auto_assign_wid takes the lowest free padded slot;
  3. next_worker_id (`workers add`) takes the lowest free NUMBER: a legacy unpadded `worker1` counts
     as number 1 taken, so no `worker01` appears beside it.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import epsiloneridani_worker as tc
import epsiloneridani_worker.worker_manager as wm

cfg = tc.config
fails = 0


def check(name, ok, detail=""):
    global fails
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + ("" if ok else f"  {detail}"))
    fails += not ok


def main():
    print("1) worker_id")
    check(
        "pads to two digits",
        [cfg.worker_id(n) for n in (1, 9, 10, 99)] == ["worker01", "worker09", "worker10", "worker99"],
    )
    check("grows past 99", cfg.worker_id(100) == "worker100")

    print("2) auto_assign_wid")
    held = {"worker01", "worker02"}
    saved = cfg.acquire_slot
    cfg.acquire_slot = lambda wid: wid not in held
    try:
        got = cfg.auto_assign_wid()  # once: it claims a slot when not stubbed
        check("lowest free padded slot", got == "worker03", got)
    finally:
        cfg.acquire_slot = saved

    print("3) next_worker_id")
    spec = lambda *ids: [wm.WorkerSpec(id=i) for i in ids]  # noqa: E731
    check("first is worker01", wm.next_worker_id([]) == "worker01")
    check("fills a gap", wm.next_worker_id(spec("worker01", "worker03")) == "worker02")
    legacy = wm.next_worker_id(spec("worker1", "worker03"))
    check("a legacy worker1 takes number 1", legacy == "worker02", legacy)
    check("other ids do not count", wm.next_worker_id(spec("update-check", "workerX")) == "worker01")

    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
