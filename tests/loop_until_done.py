#!/usr/bin/env python3
"""`--until-done` ends a targeted loop once its PRs are no longer open, and only then."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import epsiloneridani_worker as tc
from epsiloneridani_worker import github, loop

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}: got {got!r} want {want!r}")


def gh_answering(states):
    """A gh_run whose `pulls/N` lookups answer from `states` (None = the call fails)."""

    def fake(argv, **_kw):
        number = int(argv[2].rsplit("/", 1)[1])
        state = states[number]
        return subprocess.CompletedProcess(argv, 1 if state is None else 0, stdout=f"{state}\n", stderr="")

    return fake


saved = github.gh_run
try:
    github.gh_run = gh_answering({1: "open", 2: "closed"})
    check("open_prs names the open ones", github.open_prs((1, 2)), {1})
    github.gh_run = gh_answering({1: "closed", 2: "closed"})
    check("open_prs is empty when all are closed", github.open_prs((1, 2)), set())
    github.gh_run = gh_answering({1: "closed", 2: None})
    check("a failed lookup is unknown, not done", github.open_prs((1, 2)), None)
finally:
    github.gh_run = saved


def run_loop(answers, until_done=True):
    """Drive cmd_loop with rounds that return no-progress; `answers` is open_prs' reply per check.
    Returns (exit code, rounds run, checks made)."""
    seen = {"rounds": 0, "checks": 0}
    replies = list(answers)

    def fake_open_prs(_prs):
        seen["checks"] += 1
        if not replies:
            raise AssertionError("the loop kept going past its scripted answers")
        return replies.pop(0)

    def fake_round(_tail):
        seen["rounds"] += 1
        if seen["rounds"] >= 5:  # a loop with no stop condition of its own: end it as Ctrl-C would
            raise KeyboardInterrupt
        return tc.EX_NOPROGRESS

    saved_loop = (loop.open_prs, loop.run_round_subprocess, loop.choose_model, loop.github_budget, loop.time.sleep)
    loop.open_prs = fake_open_prs
    loop.run_round_subprocess = fake_round
    loop.choose_model = lambda *_a, **_k: ("claude", {})
    loop.github_budget = lambda: {}
    loop.time.sleep = lambda _s: None
    try:
        args = SimpleNamespace(ignore_quota=False, bubble=False, quota_cmd=None)
        rc = loop.cmd_loop(
            args, SimpleNamespace(wid="t"), only=["fix"], agent="claude", prs=(7,), until_done=until_done
        )
    finally:
        loop.open_prs, loop.run_round_subprocess, loop.choose_model, loop.github_budget, loop.time.sleep = saved_loop
    return rc, seen["rounds"], seen["checks"]


check("stops before any round when already done", run_loop([set()]), (0, 0, 1))
check("runs rounds while open, then stops", run_loop([{7}, {7}, set()]), (0, 2, 3))
check("a failed lookup does not stop it", run_loop([None, set()]), (0, 1, 2))

# Without the flag the loop never looks the PRs up and runs until interrupted (130).
check("without the flag it runs on until interrupted", run_loop([], until_done=False), (130, 5, 0))


def cli_exit(argv):
    saved_argv = sys.argv
    sys.argv = ["epsiloneridani", *argv]
    try:
        tc.cli.main()
    except SystemExit as e:
        return str(e.code)
    finally:
        sys.argv = saved_argv
    return None


for argv in (["work", "--until-done"], ["work", "--loop", "--until-done"], ["work", "--pr", "7", "--until-done"]):
    msg = cli_exit(argv)
    check(f"{' '.join(argv)} is refused", bool(msg) and "--until-done needs --loop and --pr" in msg, True)

sys.exit(1 if fails else 0)
