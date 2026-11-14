#!/usr/bin/env python3
"""Two limits on what a Claude usage reading may authorize.

On 2026-11-11 five workers on one account opened a fresh 5-hour window together at 07:00. Each took
one reading (0% used); the usage endpoint then answered their polls with 429s, and the loop fell back
to the cached reading, which stays valid for QUOTA_TTL (an hour). Two review workers launched round
after round on that 0% while the window was exhausted by 07:21, until the entry expired at 08:03.

1. Stale readings. A cached reading older than CLAUDE_STALE_GATE_S may still BLOCK, but no longer
   AUTHORIZES a launch or a bootstrap: it waits as unknown, without a next_eligible to sleep toward.
   A young reading is unchanged.
2. Usage credits. With credits enabled, a session past a plan limit keeps running on them, and Claude
   Code has no per-client switch to refuse that. While the endpoint reports `extra_usage.is_enabled`,
   a window at or above CLAUDE_CREDIT_GUARD_PCT blocks launches until it resets; under it, or with
   credits off, or with EPSILONERIDANI_CLAUDE_ALLOW_CREDITS=1, nothing changes. Both apply to a fresh
   fetch and to a cached reading.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import epsiloneridani_worker as tc  # noqa: E402

quota = tc.quota
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}" + ("" if cond else f"  {detail}"))


def payload(session_used, weekly_used=1, credits=None, late=False):
    """`late`: both windows nearly over, so the pace curve allows high usage and only the credit
    guard can be what blocks."""
    session_left = timedelta(minutes=5) if late else timedelta(hours=4)
    weekly_left = timedelta(minutes=30) if late else timedelta(days=6)
    p = {
        "five_hour": {"utilization": session_used, "resets_at": (datetime.now(UTC) + session_left).isoformat()},
        "seven_day": {"utilization": weekly_used, "resets_at": (datetime.now(UTC) + weekly_left).isoformat()},
    }
    if credits is not None:
        p["extra_usage"] = {"is_enabled": credits, "disabled_reason": None if credits else "out_of_credits"}
    return p


def quota_obj():
    q = tc.Quota.__new__(tc.Quota)
    q._idle_notes = lambda _readings: ({}, False)
    q._store_raw = lambda *a: None
    return q


def cached(p, age):
    """The provider a cached reading of `age` seconds yields, through the real serving path."""
    return quota_obj()._from_cached_claude((p, time.time() - age))[0]


def fetched(p):
    """The provider a fresh fetch of `p` yields."""
    q = quota_obj()
    saved = quota._http_get_json
    quota._http_get_json = lambda *_a, **_k: (200, p, None)
    try:
        return q._claude_fetch("fp", "token", share=False)[0]
    finally:
        quota._http_get_json = saved


def main():
    for k in ("EPSILONERIDANI_PACE", "EPSILONERIDANI_CLAUDE_ALLOW_CREDITS"):
        os.environ.pop(k, None)
    gate = quota.CLAUDE_STALE_GATE_S

    print("1) stale readings")
    young = cached(payload(1), 60)
    check("a young permissive reading still authorizes", young.available, young.error)
    old = cached(payload(1), gate + 60)
    check("an old permissive reading no longer authorizes", not old.available)
    check("...and says why", old.error is not None and "min old" in old.error, old.error)
    check("...and names no reset to sleep toward", old.next_eligible is None)
    check("...but keeps its windows for display", [w.status for w in old.windows] == [w.status for w in young.windows])
    blocked = cached(payload(100), gate + 60)
    check(
        "an old blocking reading still blocks, as itself",
        not blocked.available and "min old" not in (blocked.error or ""),
    )

    # The failed forced refresh that served the hour-old 0% on 2026-11-11.
    q = quota_obj()
    q._cached_claude = lambda _fp: (payload(0), time.time() - 3599)
    saved = quota._http_get_json

    def rate_limited(*_a, **_k):
        return 429, {}, 600

    quota._http_get_json = rate_limited
    try:
        served = q._claude_pass("fp", "token", refresh=True)[0]
    finally:
        quota._http_get_json = saved
    check("a 429 on refresh does not launch on an hour-old 0%", not served.available, served.error)

    print("2) usage credits")
    pct = quota.CLAUDE_CREDIT_GUARD_PCT
    for label, make in (("fresh", fetched), ("cached", lambda p: cached(p, 60))):
        base = make(payload(pct + 5, late=True, credits=False))
        check(f"{label}: (premise) pacing alone allows {pct + 5:.0f}% late in the window", base.available, base.error)
        near = make(payload(pct + 5, late=True, credits=True))
        check(f"{label}: credits on and near a limit blocks", not near.available)
        check(f"{label}: ...naming the credits", near.error is not None and "credits" in near.error, near.error)
        check(
            f"{label}: ...until that window resets", near.next_eligible is not None and near.next_eligible > time.time()
        )
        weekly = make(payload(10, weekly_used=pct + 1, late=True, credits=True))
        check(
            f"{label}: the weekly window counts too",
            not weekly.available and "credits" in (weekly.error or ""),
            weekly.error,
        )
        check(f"{label}: credits on with headroom launches", make(payload(10, late=True, credits=True)).available)
        check(
            f"{label}: credits off near a limit is unchanged",
            base.available == make(payload(pct + 5, late=True)).available,
        )
    os.environ["EPSILONERIDANI_CLAUDE_ALLOW_CREDITS"] = "1"
    try:
        allowed = fetched(payload(pct + 5, late=True, credits=True))
        check("the opt-in turns the guard off", allowed.available, allowed.error)
    finally:
        os.environ.pop("EPSILONERIDANI_CLAUDE_ALLOW_CREDITS", None)
    check("a missing extra_usage field reads as credits off", not quota._extra_usage_enabled({}))
    check(
        "a non-boolean is_enabled reads as credits off",
        not quota._extra_usage_enabled({"extra_usage": {"is_enabled": "yes"}}),
    )

    print("3) usage credits and the window bootstrap")
    # A fresh session window not yet opened (idle) while the weekly window is near its limit: the gate
    # would authorize a bootstrap, not a launch. With credits on, that bootstrap only opens a window
    # whose next launch the guard refuses, so the guard withholds it too.
    weekly_reset = time.time() + 1800
    bootstrapping = quota.Provider(
        "claude",
        False,
        None,
        [
            quota.Window("session", None, None, None, quota.STATE_IDLE),
            quota.Window("weekly", pct + 5, 99.0, weekly_reset, quota.STATUS_UNDER_PACE),
        ],
        None,
        None,
        None,
        True,
        ["session"],
    )
    on = {"extra_usage": {"is_enabled": True}}
    guarded = tc.Quota._credit_guard(bootstrapping, on)
    check("credits on: a bootstrap near a limit is withheld", not guarded.bootstrap_eligible and not guarded.available)
    check("...naming the credits", "credits" in (guarded.error or ""), guarded.error)
    check("...until the near window resets", guarded.next_eligible == weekly_reset, guarded.next_eligible)
    check(
        "credits off: the bootstrap is untouched",
        tc.Quota._credit_guard(bootstrapping, {"extra_usage": {"is_enabled": False}}) is bootstrapping,
    )
    roomy = quota.Provider(
        "claude",
        False,
        None,
        [bootstrapping.windows[0], quota.Window("weekly", 10.0, 99.0, weekly_reset, quota.STATUS_UNDER_PACE)],
        None,
        None,
        None,
        True,
        ["session"],
    )
    check("credits on with headroom: the bootstrap is untouched", tc.Quota._credit_guard(roomy, on) is roomy)

    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
