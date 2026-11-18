#!/usr/bin/env python3
"""A worker's `fallback` work kinds run only when nothing under its `only` does.

`only` is a filter over one fixed cascade, so listing review and roadmap together never makes either a
fallback: review always comes first. On 2026-11-16/17 worker27 (`only = ["roadmap", "fix-ci", "fix"]`)
opened ~20 roadmap PRs, hit the open-PR limit, and sat idle for most of a day while those PRs waited
for review. Pinned:

  1. No fallback: a backpressured roadmap worker stops with NoProgress, as before.
  2. fallback = review: it reviews one PR instead; and it does so whenever `only` finds nothing at all,
     not just under backpressure.
  3. The fallback never outranks `only`: fix work still comes first, and without backpressure it authors.
  4. Several fallback kinds run in cascade order, and roadmap can be one (a reviewer that authors when
     the queue is empty); backpressure still refuses a fallback roadmap.
  5. Kinds already in `only` are not repeated; an empty queue, a --pr round, and claimed candidates
     behave as before.
  6. workers.toml: `fallback` is validated, needs `only`, round-trips, and reaches the work argv.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import epsiloneridani_worker as tc  # noqa: E402

wu = tc.work_units
wm = tc.worker_manager
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")
    fails += not ok


def survey(*, backpressure=False, reviews=(11,), fixes=(), lint=()):
    sv = tc.Survey(worker_id="test")
    sv.roadmap_backpressure = backpressure
    sv.n_mine_open = tc.MAX_OPEN_PRS if backpressure else 0
    for n in reviews:
        sv.reviewable.actionable.append(tc.Candidate(n, "a" * 40, "awaiting review"))
    for n in fixes:
        sv.needs_fix.actionable.append(tc.Candidate(n, "b" * 40, "findings"))
    for n in lint:
        sv.lint_repair.actionable.append(tc.Candidate(n, "c" * 40, "lint"))
    return sv


def run(sv, only, fallback=(), *, prs=(), declines=()):
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
        opts = SimpleNamespace(only=only, fallback=tuple(fallback), dry_run=True, prs=prs)
        wu.run_round(SimpleNamespace(cfg=None, gh=None, rs=None, counters=None), opts)
        return seen, False
    except tc.config.NoProgress:
        return seen, True
    finally:
        wu.survey, wu.dispatch, wu.order_review_candidates = saved


AUTHOR = ["roadmap", "fix-ci", "fix"]

print("1) no fallback")
check("idles at the limit", run(survey(backpressure=True), AUTHOR), ([], True))

print("2) fallback = review")
check("reviews under backpressure", run(survey(backpressure=True), AUTHOR, ["review"]), ([("review", 11)], False))
check("...and whenever only finds nothing", run(survey(), ["fix"], ["review"]), ([("review", 11)], False))

print("3) never outranks only")
check("fix work still first", run(survey(backpressure=True, fixes=(5,)), AUTHOR, ["review"]), ([("fix", 5)], False))
check("without backpressure it authors", run(survey(), AUTHOR, ["review"]), ([("roadmap", 0)], False))

print("4) several kinds, and roadmap as a fallback")
check(
    "cascade order within the tier (lint-repair before review)",
    run(survey(lint=(7,)), ["fix"], ["review", "lint-repair"]),
    ([("lint-repair", 7)], False),
)
check(
    "a reviewer authors when the queue is empty",
    run(survey(reviews=()), ["review"], ["roadmap"]),
    ([("roadmap", 0)], False),
)
check(
    "backpressure refuses a fallback roadmap too",
    run(survey(reviews=(), backpressure=True), ["review"], ["roadmap"]),
    ([], True),
)

print("5) unchanged cases")
check(
    "a kind already in only is not repeated",
    run(survey(reviews=(11,), backpressure=True), ["review", "roadmap"], ["review"]),
    ([("review", 11)], False),
)
check("nothing anywhere: NoProgress", run(survey(reviews=(), backpressure=True), AUTHOR, ["review"]), ([], True))
check("--pr round never falls back", run(survey(backpressure=True), AUTHOR, ["review"], prs=(11,))[0], [])
check(
    "a claimed review moves on",
    run(survey(backpressure=True, reviews=(11, 12)), AUTHOR, ["review"], declines=(11,)),
    ([("review", 11), ("review", 12)], False),
)
check(
    "all claimed: NoProgress",
    run(survey(backpressure=True), AUTHOR, ["review"], declines=(11,)),
    ([("review", 11)], True),
)

print("6) workers.toml")
spec = wm.WorkerSpec.from_dict({"id": "worker27", "only": AUTHOR, "fallback": ["review"]}, 0)
check("parsed", spec.fallback, ("review",))
check("round-trips", wm.WorkerSpec.from_dict(spec.as_dict(), 0).fallback, ("review",))
argv = spec.work_argv()
check("reaches the work argv", argv[argv.index("--fallback") + 1] if "--fallback" in argv else None, "review")
for raw, why in (
    ({"id": "w", "only": ["fix"], "fallback": ["nope"]}, "unknown task"),
    ({"id": "w", "fallback": ["review"]}, "needs only"),
):
    try:
        wm.WorkerSpec.from_dict(raw, 0)
        check(f"rejects: {why}", "accepted", "rejected")
    except wm.WorkersError as exc:
        check(f"rejects: {why}", why in str(exc), True)

print(f"\n{fails} failure(s)")
sys.exit(1 if fails else 0)
