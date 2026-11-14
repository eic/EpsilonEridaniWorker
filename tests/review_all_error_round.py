#!/usr/bin/env python3
"""A review round in which no rubric got a model answer is not a review, whatever the engine exits.

On 2026-11-11 the shared Claude account's 5-hour window ran out at 07:21. Claude Code reported it as
"You've hit your 5-hour limit", wording the engine's classifier did not know, so the engine's own
provider-down abort (REVIEW_PROVIDER_DOWN_EXIT) never fired. Each round posted an all-`error`
scoreboard and exited 0; do_review took that as a posted review, cleared the PR's error streak, and
the loop claimed the next PR. Two workers did that about 55 times in 40 minutes.

The engine's classifier is fixed separately; this is the worker's backstop for the next wording it
does not know. A round that ran rubrics and used zero tokens cannot have reviewed anything. Pinned:

  1. Such a round raises NoProgress (the loop backs off), says it is not charged, and charges no
     review error.
  2. A round that used tokens is a review as before: returns 0, clears the streak.
  3. Rounds the backstop must not judge stay successes: nothing ran (all carried), an older round
     than this run (the engine wrote no ledger entry), another head or none named, an unreadable
     ledger.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import datetime
import json
import sys
import tempfile
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import epsiloneridani_worker as tc  # noqa: E402

wu = tc.work_units
fails = 0
HEAD = "a" * 40
ZERO = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0}
SPENT = {"input_tokens": 126, "cached_input_tokens": 0, "output_tokens": 77012, "reasoning_output_tokens": 0}


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


class Counters:
    def __init__(self):
        self.v = {"review-err-42": 1}

    def read(self, k):
        return self.v.get(k, 0)

    def incr(self, k):
        self.v[k] = self.v.get(k, 0) + 1
        return self.v[k]

    def write(self, k, n):
        self.v[k] = n


def iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.UTC).isoformat()


def drive(state, ledger_round):
    """Run do_review with an engine stub that exits 0 after writing `ledger_round` (None: writes
    nothing; "garbage": an unreadable ledger). Returns (counters, raised, rc)."""
    store = state / "store"
    store.mkdir(exist_ok=True)
    w = types.SimpleNamespace(
        cfg=types.SimpleNamespace(state=state, logdir=state, wid="w", store_dir=store),
        counters=Counters(),
        rs=types.SimpleNamespace(bust=lambda pr: None, review_rounds=lambda pr, c: 0),
        gh=types.SimpleNamespace(add_reaction=lambda i: True, remove_reaction=lambda i: True),
    )
    c = types.SimpleNamespace(pr=42, head=HEAD, contest=None, contest_reply_id=None)
    opts = types.SimpleNamespace(work_model="claude")

    def engine(*a, **k):
        if ledger_round == "garbage":
            (store / "ledger.json").write_text("{not json")
        elif ledger_round is not None:
            (store / "ledger.json").write_text(json.dumps({"days": {}, "prs": {"42": {"rounds": [ledger_round]}}}))
        return 0

    saved = wu.run_to_logfile, wu._sync_review_outbox, wu.me
    wu.run_to_logfile, wu._sync_review_outbox, wu.me = engine, (lambda *a, **k: 0), (lambda: "tester")
    try:
        return w.counters, None, wu.do_review(w, types.SimpleNamespace(), c, opts, bubble=False)
    except tc.config.NoProgress as e:
        return w.counters, e, None
    finally:
        wu.run_to_logfile, wu._sync_review_outbox, wu.me = saved
        (store / "ledger.json").unlink(missing_ok=True)


def rnd(**over):
    base = {
        "round": 1,
        "ts": iso(time.time()),
        "head_sha": HEAD,
        "ran": ["reuse", "naming"],
        "tokens": ZERO,
        "cost": 0.0,
        "states": {"reuse": "error", "naming": "error"},
    }
    base.update(over)
    return base


def main():
    with tempfile.TemporaryDirectory() as d:
        state = Path(d)

        print("1) every rubric failed without a token")
        ctr, raised, _ = drive(state, rnd())
        check("raises NoProgress", raised is not None)
        check("says it is not charged", raised is not None and "not charged" in str(raised))
        check("charges no review error (the old streak is left as it was)", ctr.read("review-err-42") == 1)

        print("2) a real review")
        ctr, raised, rc = drive(state, rnd(tokens=SPENT, cost=20.6, states={"reuse": "approve"}))
        check("returns 0", raised is None and rc == 0)
        check("clears the streak", ctr.read("review-err-42") == 0)

        print("3) rounds the backstop must not judge")
        cases = {
            "nothing ran (all carried)": rnd(ran=[]),
            "an older round than this run": rnd(ts=iso(time.time() - 3600)),
            "another head": rnd(head_sha="b" * 40),
            "a round that names no head": rnd(head_sha=None),
            "no token counts recorded": rnd(tokens={}),
            "no ledger entry at all": None,
            "an unreadable ledger": "garbage",
        }
        for name, case in cases.items():
            _, raised, rc = drive(state, case)
            check(f"{name}: a success as before", raised is None and rc == 0)

    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
