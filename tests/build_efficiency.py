#!/usr/bin/env python3
"""Host rounds spend less of themselves on Lean builds.

Measured on 37 fix rounds since 2026-10-25: half of a round's wall time was builds, 56 of 61 agent
sessions hit the host's thread-creation limit, and cache reads of build logs were the biggest cost.
Two changes, pinned here:

  1. prefetch_own_outputs: before the agent starts, a host round fetches EpsilonEridani's own
     main-built outputs from the public Lake cache, with a config the WORKER writes (never a script
     from the checkout, which on a fix round is the PR's branch). Best effort: a miss, a failure, a
     timeout or a missing `lake` only logs; $EPSILONERIDANI_OWN_CACHE=0 turns it off; the temporary
     config is removed. Both host call sites fetch before launching the agent.
  2. Every shipped prompt tells the agent to rebuild single modules while iterating, filter its build
     logs, and run the full commands once before pushing, prefix-aware.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
import epsiloneridani_worker as tc

agents = tc.agents
fails = 0


def check(name, ok, detail=""):
    global fails
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + ("" if ok else f"  {detail}"))
    fails += not ok


def run_prefetch(result=None, raises=None, which="/usr/bin/lake", env=None):
    seen, logs = {}, []

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        seen["config_text"] = Path(kw["env"]["LAKE_CONFIG"]).read_text()
        seen["config_path"] = kw["env"]["LAKE_CONFIG"]
        if raises:
            raise raises
        return result

    saved = (agents.subprocess.run, agents.shutil.which, agents.log)
    agents.subprocess.run = fake_run
    agents.shutil.which = lambda name: which if name == "lake" else None
    agents.log = logs.append
    old_env = os.environ.get(agents.OWN_CACHE_ENV)
    if env is None:
        os.environ.pop(agents.OWN_CACHE_ENV, None)
    else:
        os.environ[agents.OWN_CACHE_ENV] = env
    try:
        agents.prefetch_own_outputs(Path(tempfile.gettempdir()))
    finally:
        agents.subprocess.run, agents.shutil.which, agents.log = saved
        if old_env is None:
            os.environ.pop(agents.OWN_CACHE_ENV, None)
        else:
            os.environ[agents.OWN_CACHE_ENV] = old_env
    return seen, logs


def main():
    print("1) the prefetch")
    ok = subprocess.CompletedProcess([], 0, "", "")
    seen, logs = run_prefetch(ok)
    cmd = seen.get("cmd", [])
    check(
        "runs lake cache get for EpsilonEridani",
        cmd[:3] == ["lake", "cache", "get"] and "--repo" in cmd and agents.EPSILONERIDANI in cmd,
        cmd,
    )
    check("walks back --max-revs", f"--max-revs={agents.OWN_CACHE_MAX_REVS}" in cmd)
    check("in the checkout", seen["kw"].get("cwd") == Path(tempfile.gettempdir()))
    check("bounded", seen["kw"].get("timeout") == agents.OWN_CACHE_TIMEOUT_S)
    cfg = seen.get("config_text", "")
    check(
        "the worker writes the service config",
        agents.EPSILONERIDANI_CACHE_ARTIFACT_ENDPOINT in cfg
        and agents.EPSILONERIDANI_CACHE_REVISION_ENDPOINT in cfg
        and f'name = "{agents.EPSILONERIDANI_CACHE_SERVICE}"' in cfg,
        cfg,
    )
    check("the temporary config is removed", not Path(seen["config_path"]).exists())
    check("a hit is logged", any("fetched EpsilonEridani's own outputs" in m for m in logs), logs)

    _, logs = run_prefetch(subprocess.CompletedProcess([], 1, "", "error: no outputs found"))
    check("a miss is logged as a miss", any("no published EpsilonEridani outputs" in m for m in logs), logs)
    _, logs = run_prefetch(subprocess.CompletedProcess([], 1, "", "error: eic/EpsilonEridani: output lookup failed"))
    check(
        "a failure is logged, not raised",
        any("fetch failed (error: eic/EpsilonEridani: output lookup failed)" in m for m in logs),
        logs,
    )
    _, logs = run_prefetch(raises=subprocess.TimeoutExpired(["lake"], 1))
    check("a timeout is logged, not raised", any("did not finish (TimeoutExpired)" in m for m in logs), logs)
    seen, _ = run_prefetch(ok, env="0")
    check("off with EPSILONERIDANI_OWN_CACHE=0", "cmd" not in seen)
    seen, _ = run_prefetch(ok, which=None)
    check("skipped without lake", "cmd" not in seen)

    print("2) both host call sites fetch before the agent")
    src = (REPO / "epsiloneridani_worker" / "work_units.py").read_text()
    for marker in (
        "prefetch_own_outputs(co)\n        rc = run_agent_host(co,",
        "prefetch_own_outputs(w.cfg.checkout)\n    return run_agent_host(w.cfg.checkout,",
    ):
        check(f"call site: {marker.split(chr(10))[0].strip()}", marker in src)

    print("3) every prompt teaches the iteration loop")
    for p in sorted((tc.paths.HERE / "prompts").glob("*.md")):
        t = p.read_text()
        check(f"{p.name} says to rebuild single modules", "`__BUILD__lake build EpsilonEridani.Some.Module`" in t)
        check(f"{p.name} says to filter logs", re.search(r"grep -E 'error\|✖' \| head -40", t) is not None)
        check(f"{p.name} keeps the full run before pushing", "Run the full commands above once" in t)

    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
