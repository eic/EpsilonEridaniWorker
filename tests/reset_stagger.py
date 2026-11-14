#!/usr/bin/env python3
"""Workers held by quota wake up staggered after a reset, not all at once.

Every worker on one account sleeps toward the same moment, the reset its blocking window names. On
2026-11-11 five workers on one Claude account woke within two minutes of a 07:00 reset and launched
two reviews and three fix rounds together, each on a fresh reading of 0% used; the 5-hour window was
gone by 07:21. Pinned here:

  1. reset_stagger_s is fixed per worker id, inside [0, spread), and 0 when the spread is 0.
  2. The five ids that shared that account land at least a minute apart over the default spread
     (a hash of the id put three of them within 40 s), and $EPSILONERIDANI_RESET_OFFSET_S overrides
     the computed offset; a malformed value is ignored.
  3. quota_nap adds the offset only when sleeping toward a named moment: a Retry-After is honoured as
     asked, an hourly recheck still caps a long wait, and with nothing named it polls as before.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import epsiloneridani_worker as tc  # noqa: E402

loop = tc.loop
Provider = tc.quota.Provider
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}" + ("" if cond else f"  {detail}"))


def held(next_eligible=None, retry_after=None):
    return {"claude": Provider("claude", False, None, [], None, next_eligible, retry_after)}


def main():
    os.environ.pop("EPSILONERIDANI_RESET_OFFSET_S", None)
    spread = loop.RESET_STAGGER_S

    print("1) the offset")
    check("the default spread is on", spread > 0, spread)
    check("fixed per id", loop.reset_stagger_s("worker05") == loop.reset_stagger_s("worker05"))
    ids = [f"worker{n:02d}" for n in range(1, 40)] + ["default", "laptop"]
    check("inside [0, spread)", all(0 <= loop.reset_stagger_s(w) < spread for w in ids))
    check("a spread of 0 turns it off", loop.reset_stagger_s("worker05", 0) == 0)

    print("2) the ids that shared one account on 2026-11-11")
    shared = sorted(loop.reset_stagger_s(w) for w in ("worker01", "worker03", "worker04", "worker05", "worker06"))
    gaps = [b - a for a, b in zip(shared, shared[1:], strict=False)]
    check("at least a minute apart", min(gaps) >= 60, shared)
    check("spread over most of the window", shared[-1] - shared[0] >= spread / 2, shared)
    os.environ["EPSILONERIDANI_RESET_OFFSET_S"] = "123"
    check("an explicit offset wins", loop.reset_stagger_s("worker05") == 123)
    os.environ["EPSILONERIDANI_RESET_OFFSET_S"] = "soon"
    check(
        "a malformed one is ignored, falling back to the computed offset",
        loop.reset_stagger_s("worker05") == int(((5 * loop._GOLDEN) % 1.0) * spread),
    )
    os.environ.pop("EPSILONERIDANI_RESET_OFFSET_S", None)

    print("3) quota_nap")
    now = 1_000_000.0
    off = loop.reset_stagger_s("worker05")
    # 658 s: what every worker slept toward the 07:00 reset on 2026-11-11.
    nap = loop.quota_nap(held(next_eligible=now + 658), "worker05", now)
    check("toward a named reset: the line plus this worker's offset", nap == 658 + 5 + off, nap)
    other = loop.quota_nap(held(next_eligible=now + 658), "worker03", now)
    check("another worker wakes at another time", other != nap, (nap, other))
    check(
        "a Retry-After is honoured as asked, unstaggered",
        loop.quota_nap(held(next_eligible=now + 100, retry_after=580), "worker05", now) == 580,
    )
    check("a long wait still rechecks hourly", loop.quota_nap(held(next_eligible=now + 86400), "worker05", now) == 3600)
    check(
        "a reset closer than the poll keeps the poll floor",
        loop.quota_nap(held(next_eligible=now + 10), "worker28", now) >= tc.constants.POLL,
    )
    check("nothing named: the normal poll", loop.quota_nap(held(), "worker05", now) == tc.constants.POLL)

    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
