#!/usr/bin/env python3
"""A bump round skips a head whose build fails only inside dependencies.

On 2026-11-08 bump PR #163 failed in Physlib's own Velocity/Basic.lean (a mathlib refactor Physlib had
not caught up with), and three Opus bump rounds were spent on a failure nothing in EpsilonEridani/ could
fix. bump_triage reads the failing build's log; this pins:

  1. failing modules come from Lake's `✖ [i/n] Building|Running <Module>` lines, deduplicated;
  2. "dependency only" needs at least one failure and no EpsilonEridani module among them;
  3. the run is the one behind the newest `build` commit status;
  4. anything unreadable (status, log) answers None, i.e. run the round as before;
  5. do_bump on a dependency-only head spends that HEAD's budget, leaves the PR-wide budget alone,
     and never launches the agent; otherwise it proceeds as before.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import epsiloneridani_worker as tc

bt = tc.bump_triage
wu = tc.work_units
fails = 0

PHYSLIB_ONLY = """\
✔ [9291/9383] Built EpsilonEridani.QFT.PerturbationTheory.FeynmanDiagrams.BasicExtensions (6.4s)
✖ [9292/9383] Building Physlib.Relativity.Tensors.RealTensor.Velocity.Basic (5.7s)
error: Physlib/Relativity/Tensors/RealTensor/Velocity/Basic.lean:27:46: Type mismatch
✖ [9293/9383] Building Physlib.Relativity.Tensors.RealTensor.Velocity.Basic (5.7s)
error: build failed
"""
MIXED = """\
✖ [9264/9358] Running Physlib.Relativity.Tensors.RealTensor.Vector.Causality.Basic
✖ [9265/9358] Running EpsilonEridani.QFT.Lattice.Continuation.Basic
error: EpsilonEridani/QFT/Lattice/Continuation/Basic.lean: bad import
"""


def check(name, ok, detail=""):
    global fails
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + ("" if ok else f"  {detail}"))
    fails += not ok


def main():
    print("1-2) parsing")
    mods = bt.failing_modules(PHYSLIB_ONLY)
    check("deduplicated failing modules", mods == ["Physlib.Relativity.Tensors.RealTensor.Velocity.Basic"], mods)
    check("Physlib only is dependency only", bt.dependency_only(mods))
    mixed = bt.failing_modules(MIXED)
    check("Running lines count too", len(mixed) == 2, mixed)
    check("an EpsilonEridani failure is ours to adapt", not bt.dependency_only(mixed))
    check("nothing parsed is not dependency only", not bt.dependency_only(bt.failing_modules("error: build failed")))
    check("a lookalike prefix is not ours", bt.dependency_only(["EpsilonEridaniX.Foo"]))
    uni = bt.failing_modules("✖ [1/2] Building Physlib.Ωmega.«weird name» (1.0s)\n✖ [2/2] Building Λ.Basic\n")
    check("non-ASCII and guillemet names parse", uni == ["Physlib.Ωmega.«weird name»", "Λ.Basic"], uni)

    print("3) the run behind the newest build status")
    statuses = [
        {"context": "bump-guard", "target_url": "https://github.com/o/r/actions/runs/1"},
        {"context": "build", "state": "failure", "target_url": "https://github.com/o/r/actions/runs/222/job/9"},
        {"context": "build", "state": "failure", "target_url": "https://github.com/o/r/actions/runs/111"},
    ]
    check("newest build status wins", bt.build_run_id(statuses) == "222", bt.build_run_id(statuses))
    check("no build status, no run", bt.build_run_id(statuses[:1]) is None)
    green = [{"context": "build", "state": "success", "target_url": "https://github.com/o/r/actions/runs/5"}]
    check("a green head has no run to read", bt.build_run_id(green) is None)
    check("an errored build is read", bt.build_run_id([{**green[0], "state": "error"}]) == "5")

    print("4) reading GitHub")

    def gh_stub(status_rc=0, statuses_json=None, log_rc=0, log=PHYSLIB_ONLY):
        def fake(argv, **kw):
            if argv[:2] == ["gh", "api"]:
                return subprocess.CompletedProcess(argv, status_rc, json.dumps(statuses_json or statuses), "")
            return subprocess.CompletedProcess(argv, log_rc, log, "")

        return fake

    saved = bt.gh_run
    try:
        bt.gh_run = gh_stub()
        check(
            "dependency-only head",
            bt.dependency_failure("o/r", "h") == ["Physlib.Relativity.Tensors.RealTensor.Velocity.Basic"],
        )
        bt.gh_run = gh_stub(log=MIXED)
        check("mixed head runs the round", bt.dependency_failure("o/r", "h") is None)
        bt.gh_run = gh_stub(status_rc=1)
        check("unreadable statuses run the round", bt.dependency_failure("o/r", "h") is None)
        bt.gh_run = gh_stub(log_rc=1)
        check("unreadable log runs the round", bt.dependency_failure("o/r", "h") is None)
        bt.gh_run = gh_stub(statuses_json=[{"context": "build", "state": "failure", "target_url": ""}])
        check("a status without a run runs the round", bt.dependency_failure("o/r", "h") is None)
        fetched = []

        def no_log_for_green(argv, **kw):
            if argv[:2] == ["gh", "api"]:
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps([{"context": "build", "state": "success", "target_url": "/actions/runs/9"}]), ""
                )
            fetched.append(argv)
            return subprocess.CompletedProcess(argv, 0, PHYSLIB_ONLY, "")

        bt.gh_run = no_log_for_green
        check("a green head fetches no log", bt.dependency_failure("o/r", "h") is None and not fetched, fetched)
    finally:
        bt.gh_run = saved

    print("5) do_bump")
    counters, writes, logs, launched = {}, {}, [], []

    class Counters:
        def incr(self, k):
            counters[k] = counters.get(k, 0) + 1

        def write(self, k, v):
            writes[k] = v

    w = SimpleNamespace(counters=Counters())
    c = SimpleNamespace(pr=163, head="6e1b37e3b0f2" + "0" * 28)
    saved = (wu.dependency_failure, wu.log, wu._do_fixlike)
    wu.log = logs.append
    wu._do_fixlike = lambda *a, **k: launched.append(k) or 0
    try:
        wu.dependency_failure = lambda repo, head: ["Physlib.A", "Physlib.B", "Physlib.C", "Physlib.D"]
        rc = wu.do_bump(w, None, c, None, False)
        check("skipped", rc is None and not launched, (rc, launched))
        check(
            "this head's budget is spent",
            writes.get(f"bump-163-{c.head[:12]}") == tc.constants.MAX_BUMP_ATTEMPTS,
            writes,
        )
        check("the PR-wide budget is untouched", "bump-pr-163" not in counters and "bump-pr-163" not in writes)
        check(
            "the reason is logged",
            any("fails only inside dependencies (Physlib.A, Physlib.B, Physlib.C, …)" in m for m in logs),
            logs,
        )
        wu.dependency_failure = lambda repo, head: None
        rc = wu.do_bump(w, None, c, None, False)
        check("otherwise the round runs", launched and launched[-1].get("prompt_file") == "bump.md", launched)
        check("and charges both budgets", counters.get("bump-pr-163") == 1)
    finally:
        wu.dependency_failure, wu.log, wu._do_fixlike = saved

    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
