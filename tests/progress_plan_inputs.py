#!/usr/bin/env python3
"""A progress round decides on fresh inputs: a current `origin/docgen` and a reachable `--min-prs`.

No progress report had landed by 2026-11-08. `plan` refuses when the documentation was built from a
commit the checkout's `origin/docgen` does not reach (one stale copy read as "not due"), and its
default of 10 merged PRs per area was more than any area had. This pins:

  1. the round fetches exactly `origin/docgen` into the checkout, before `plan`;
  2. `plan` gets `--min-prs` from PROGRESS_MIN_PRS (default 5, $EPSILONERIDANI_PROGRESS_MIN_PRS);
  3. a failed docgen fetch is logged and `plan` still decides with what is there.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import epsiloneridani_worker as tc

wu = tc.work_units
fails = 0


def check(name, ok, detail=""):
    global fails
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + ("" if ok else f"  {detail}"))
    fails += not ok


def drive(docgen_rc=0):
    state = Path(tempfile.mkdtemp(prefix="progress-plan-"))
    (state / "progress" / "roadmap" / ".git").mkdir(parents=True)
    calls, logs = [], []

    def fake_run(cmd, **kw):
        calls.append([str(c) for c in cmd])
        if "refs/heads/docgen:refs/remotes/origin/docgen" in " ".join(map(str, cmd)):
            return subprocess.CompletedProcess(cmd, docgen_rc, "", "fatal: unable to access")
        if "plan" in cmd:
            return subprocess.CompletedProcess(cmd, wu.EX_NOPROGRESS, "", "not due: test")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    w = SimpleNamespace(
        cfg=SimpleNamespace(state=state, checkout=state / "co"),
        counters=SimpleNamespace(write=lambda *a: None, incr=lambda *a: None),
    )
    saved = (wu.subprocess.run, wu.prepare_checkout, wu.bust_progress_cache, wu.log)
    wu.subprocess.run, wu.prepare_checkout = fake_run, lambda cfg: True
    wu.bust_progress_cache, wu.log = (lambda cfg: None), logs.append
    try:
        rc = wu._do_progress_inner(w, SimpleNamespace())
    finally:
        wu.subprocess.run, wu.prepare_checkout, wu.bust_progress_cache, wu.log = saved
        shutil.rmtree(state, ignore_errors=True)
    return rc, calls, logs


def min_prs_in_fresh_interpreter(value):
    """PROGRESS_MIN_PRS as a fresh import sees it, with the variable unset (None) or set: the
    constant is read at import, so only a new interpreter tests the default deterministically."""
    env = {k: v for k, v in os.environ.items() if k != "EPSILONERIDANI_PROGRESS_MIN_PRS"}
    if value is not None:
        env["EPSILONERIDANI_PROGRESS_MIN_PRS"] = value
    code = f"import sys; sys.path.insert(0, {str(REPO)!r}); from epsiloneridani_worker import constants as c; print(c.PROGRESS_MIN_PRS)"
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    return r.stdout.strip()


def drive_timeout():
    state = Path(tempfile.mkdtemp(prefix="progress-plan-"))
    (state / "progress" / "roadmap" / ".git").mkdir(parents=True)
    calls, logs = [], []

    def fake_run(cmd, **kw):
        calls.append([str(c) for c in cmd])
        if "refs/heads/docgen:refs/remotes/origin/docgen" in " ".join(map(str, cmd)):
            assert kw.get("timeout") and kw.get("errors") == "replace", kw
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        if "plan" in cmd:
            return subprocess.CompletedProcess(cmd, wu.EX_NOPROGRESS, "", "not due: test")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    w = SimpleNamespace(
        cfg=SimpleNamespace(state=state, checkout=state / "co"),
        counters=SimpleNamespace(write=lambda *a: None, incr=lambda *a: None),
    )
    saved = (wu.subprocess.run, wu.prepare_checkout, wu.bust_progress_cache, wu.log)
    wu.subprocess.run, wu.prepare_checkout = fake_run, lambda cfg: True
    wu.bust_progress_cache, wu.log = (lambda cfg: None), logs.append
    try:
        wu._do_progress_inner(w, SimpleNamespace())
    finally:
        wu.subprocess.run, wu.prepare_checkout, wu.bust_progress_cache, wu.log = saved
        shutil.rmtree(state, ignore_errors=True)
    return calls, logs


def main():
    print("1-2) fresh docgen, reachable --min-prs")
    rc, calls, logs = drive()
    joined = [" ".join(c) for c in calls]
    docgen = next((i for i, c in enumerate(joined) if "refs/heads/docgen:refs/remotes/origin/docgen" in c), None)
    plan = next((i for i, c in enumerate(calls) if "plan" in c), None)
    check("nothing due ends the round quietly", rc is None, rc)
    check("origin/docgen is fetched", docgen is not None, joined)
    check(
        "into the checkout",
        docgen is not None and f"-C {Path(calls[docgen][2])}" in joined[docgen] and calls[docgen][2].endswith("/co"),
    )
    check("before plan", docgen is not None and plan is not None and docgen < plan, (docgen, plan))
    pc = calls[plan] if plan is not None else []
    check("plan gets --min-prs", "--min-prs" in pc and pc[pc.index("--min-prs") + 1] == str(wu.PROGRESS_MIN_PRS), pc)
    check("the default is 5", min_prs_in_fresh_interpreter(None) == "5", min_prs_in_fresh_interpreter(None))
    check("the variable overrides it", min_prs_in_fresh_interpreter("7") == "7", min_prs_in_fresh_interpreter("7"))

    print("3) a failed docgen fetch does not stop the decision")
    calls_t, logs_t = drive_timeout()
    check(
        "a hung fetch is bounded and logged",
        any("could not refresh origin/docgen (TimeoutExpired)" in m for m in logs_t),
        logs_t,
    )
    check("plan still runs after it", any("plan" in c for c in calls_t))
    rc, calls, logs = drive(docgen_rc=1)
    check("plan still runs", any("plan" in c for c in calls))
    check("the failure is logged", any("could not refresh origin/docgen" in m for m in logs), logs)

    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
