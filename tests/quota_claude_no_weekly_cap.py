#!/usr/bin/env python3
"""$EPSILONERIDANI_CLAUDE_NO_WEEKLY_CAP: some Claude seats only ever meter the 5-hour session window —
their usage endpoint's weekly fields stay null FOREVER, not just through the post-reset gap the
bootstrap (quota_claude_bootstrap.py) is for. Nothing in a single reading can tell those two apart, so
this is an explicit operator declaration, not an inference. Without it, an always-null weekly window
reads as an idle window that never resolves: `claude` never goes available and the bootstrap keeps
retrying once an hour, forever, even though the session window it actually reports is healthy.

Properties under test:
  - by default, a payload with no weekly record still gates on it (idle, not available) — the flag is
    OFF unless the operator turns it on;
  - with the flag on, the weekly window is dropped entirely: not read, not idle, not bootstrap-eligible,
    and availability is decided from the session window alone;
  - the flag is read live from the env, like the other operator dials (roadmap_only, auto_refresh), so
    it can be toggled between calls without reconstructing anything.

Dependency-free; no network.
"""

import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import epsiloneridani_worker as tc

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")
    fails += not ok


os.environ["EPSILONERIDANI_PACE"] = "0:0,100:100"  # identity curve: budget == elapsed%
os.environ.pop("EPSILONERIDANI_CLAUDE_NO_WEEKLY_CAP", None)

q = tc.Quota.__new__(tc.Quota)  # the pure parse only reaches self for a staticmethod


def iso(delta_s: float) -> str:
    return datetime.fromtimestamp(time.time() + delta_s, tz=UTC).isoformat().replace("+00:00", "Z")


# A live session window, well under pace: 20% elapsed, 10% used.
SESSION_LIVE = {"utilization": 10, "resets_at": iso(4 * 3600)}

# This account's real shape (see the live payload this was written against): five_hour present and
# active, seven_day (and every weekly-shaped key) explicitly null, and `limits` carries only a session
# entry — never a weekly one, however long the account is used.
NO_WEEKLY_PAYLOAD = {
    "five_hour": SESSION_LIVE,
    "seven_day": None,
    "limits": [{"kind": "session", "group": "session", "percent": 10, "resets_at": SESSION_LIVE["resets_at"]}],
}

# --- off by default: the payload still gates on the never-populated weekly window -----------------
p = q._claude_from_payload(NO_WEEKLY_PAYLOAD)
check("flag off: weekly is read and reported idle", [w.name for w in p.windows], ["session", "weekly"])
check("flag off: claude is NOT available", p.available, False)
check(
    "flag off: idle window is flagged bootstrap-eligible",
    (p.bootstrap_eligible, p.pending_bootstrap),
    (True, ["weekly"]),
)

# --- on: the weekly window is dropped, not merely ignored ------------------------------------------
os.environ["EPSILONERIDANI_CLAUDE_NO_WEEKLY_CAP"] = "1"
p = q._claude_from_payload(NO_WEEKLY_PAYLOAD)
check("flag on: only the session window is read", [w.name for w in p.windows], ["session"])
check("flag on: claude IS available on a healthy session window alone", (p.available, p.model), (True, "opus"))
check("flag on: nothing is left to bootstrap", (p.bootstrap_eligible, p.pending_bootstrap), (False, []))

# A session window that's actually over budget must still block — the flag drops weekly, not pacing.
# (No stale `limits` entry here: that array is authoritative over the flat key, so overriding only
# `five_hour` while leaving the old `limits` percent behind would test the wrong record.)
OVER_BUDGET_SESSION = {"utilization": 90, "resets_at": iso(4 * 3600)}  # 20% elapsed, 90% used
p = q._claude_from_payload({"five_hour": OVER_BUDGET_SESSION, "seven_day": None})
check("flag on: an over-pace session still blocks", p.available, False)

# --- live, per-call: toggling the env is seen without reconstructing anything ----------------------
os.environ.pop("EPSILONERIDANI_CLAUDE_NO_WEEKLY_CAP", None)
p = q._claude_from_payload(NO_WEEKLY_PAYLOAD)
check("turning the flag back off is picked up live", [w.name for w in p.windows], ["session", "weekly"])

os.environ.pop("EPSILONERIDANI_CLAUDE_NO_WEEKLY_CAP", None)
os.environ.pop("EPSILONERIDANI_PACE", None)

print(f"\n{'PASS' if fails == 0 else f'FAIL ({fails})'}")
sys.exit(1 if fails else 0)
