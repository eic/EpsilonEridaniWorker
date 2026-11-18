#!/usr/bin/env python3
"""A roadmap worker held by backpressure may review instead of idling, when the operator allows it.

On 2026-11-16/17 worker27 (`only = ["roadmap", "fix-ci", "fix"]`) opened ~20 roadmap PRs, hit the
open-PR limit, and then sat idle for most of a day: the limit was reached because its PRs were waiting
for review, and it was not allowed to review them. Pinned:

  1. Off by default: a backpressured roadmap-only worker still stops with NoProgress, reviewing nothing.
  2. With EPSILONERIDANI_BACKPRESSURE_REVIEW=1 it reviews one PR instead.
  3. It never outranks the stages the worker was given: fix work still comes first, and without
     backpressure it authors.
  4. A worker that already reviews is unchanged (its review turn was in the cascade).
  5. Nothing to review: NoProgress, as before. A --pr round never falls back.
  6. A review claimed elsewhere (dispatch returns None) moves on to the next candidate.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import epsiloneridani_worker as tc  # noqa: E402

wu = tc.work_units
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")
    fails += not ok


def survey(*, backpressure=True, reviews=(11,), fixes=()):
    sv = tc.Survey(worker_id="test")
    sv.roadmap_backpressure = backpressure
    sv.n_mine_open = tc.MAX_OPEN_PRS if backpressure else 0
    for n in reviews:
        sv.reviewable.actionable.append(tc.Candidate(n, "a" * 40, "awaiting review"))
    for n in fixes:
        sv.needs_fix.actionable.append(tc.Candidate(n, "b" * 40, "findings"))
    return sv


def run(sv, only, *, prs=(), declines=()):
    """run_round over a fixed survey; returns (dispatched [(stage, pr)], NoProgress raised?)."""
    seen = []

    def dispatch(stage, _w, _sv, c, _opts):
        seen.append((stage, c.pr))
        return None if c.pr in declines else 0

    saved = wu.survey, wu.dispatch, wu.order_review_candidates
    wu.survey = lambda *_a, **_k: sv
    wu.dispatch = dispatch
    wu.order_review_candidates = lambda *_a, **_k: None  # throttles/affinity need a real queue and gh
    try:
        wu.run_round(
            SimpleNamespace(cfg=None, gh=None, rs=None, counters=None),
            SimpleNamespace(only=only, dry_run=True, prs=prs),
        )
        return seen, False
    except tc.config.NoProgress:
        return seen, True
    finally:
        wu.survey, wu.dispatch, wu.order_review_candidates = saved


AUTHOR = ["roadmap", "fix-ci", "fix"]
saved_env = os.environ.get("EPSILONERIDANI_BACKPRESSURE_REVIEW")
try:
    os.environ.pop("EPSILONERIDANI_BACKPRESSURE_REVIEW", None)
    print("1) off by default")
    check("idles at the limit", run(survey(), AUTHOR), ([], True))

    os.environ["EPSILONERIDANI_BACKPRESSURE_REVIEW"] = "1"
    print("2) allowed")
    check("reviews one PR instead", run(survey(), AUTHOR), ([("review", 11)], False))

    print("3) never outranks the given stages")
    check("fix work still first", run(survey(fixes=(5,)), AUTHOR), ([("fix", 5)], False))
    check("without backpressure it authors", run(survey(backpressure=False), AUTHOR), ([("roadmap", 0)], False))

    print("4) a worker that already reviews")
    check("reviews in its cascade turn, once", run(survey(), ["review", "roadmap"]), ([("review", 11)], False))

    print("5) nothing to review, or a targeted round")
    check("no reviews: NoProgress", run(survey(reviews=()), AUTHOR), ([], True))
    check("--pr round never falls back", run(survey(), AUTHOR, prs=(11,))[0], [])

    print("6) a claimed review moves on")
    check(
        "next candidate taken",
        run(survey(reviews=(11, 12)), AUTHOR, declines=(11,)),
        ([("review", 11), ("review", 12)], False),
    )
    check("all claimed: NoProgress", run(survey(reviews=(11,)), AUTHOR, declines=(11,)), ([("review", 11)], True))

    print("dial values")
    for v, want in (("1", True), ("true", True), ("on", True), ("0", False), ("", False), ("no", False)):
        os.environ["EPSILONERIDANI_BACKPRESSURE_REVIEW"] = v
        check(f"{v!r}", wu.backpressure_review_enabled(), want)
finally:
    if saved_env is None:
        os.environ.pop("EPSILONERIDANI_BACKPRESSURE_REVIEW", None)
    else:
        os.environ["EPSILONERIDANI_BACKPRESSURE_REVIEW"] = saved_env

print(f"\n{fails} failure(s)")
sys.exit(1 if fails else 0)
