#!/usr/bin/env python3
"""$EPSILONERIDANI_CLAUDE_WEEKLY_SCOPE: some accounts carry a model-scoped weekly budget (the usage
endpoint's `weekly_scoped` entry, e.g. `scope.model.display_name = "Fable"`) on top of the overall weekly.
A worker authoring on that model should pace on its own budget: paced on the overall weekly, it is held
by spend on the other models while its own budget sits unused.

Properties under test:
  - by default the scoped cap is skipped and the overall weekly (weekly_all) gates, as before;
  - with the scope named, THAT record is the weekly window (matched by display_name or id,
    case-insensitively) and the overall weekly no longer gates this worker;
  - a named scope the payload does not carry fails closed (absent), and never falls back to the flat
    `seven_day`, which is the overall weekly, not the scope;
  - the session window still gates either way;
  - the bootstrap turn runs on the worker's authoring model when one is set, so it opens the window the
    worker will spend, and stays a bare `claude -p` turn otherwise.

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
for var in (
    "EPSILONERIDANI_CLAUDE_WEEKLY_SCOPE",
    "EPSILONERIDANI_CLAUDE_NO_WEEKLY_CAP",
    "EPSILONERIDANI_AUTHORING_CLAUDE_MODEL",
):
    os.environ.pop(var, None)

q = tc.Quota.__new__(tc.Quota)  # the pure parse only reaches self for a staticmethod


def iso(delta_s: float) -> str:
    return datetime.fromtimestamp(time.time() + delta_s, tz=UTC).isoformat().replace("+00:00", "Z")


WEEK_RESET = iso(3.5 * 86400)  # 50% of the week elapsed
SESSION = {"kind": "session", "group": "session", "percent": 10, "resets_at": iso(4 * 3600), "scope": None}


def payload(overall: int, fable: int, session=SESSION) -> dict:
    """The live shape: a session, the unscoped weekly_all and one model-scoped weekly. The flat
    seven_day mirrors the overall weekly, as the endpoint reports it."""
    return {
        "five_hour": {"utilization": session["percent"], "resets_at": session["resets_at"]},
        "seven_day": {"utilization": overall, "resets_at": WEEK_RESET},
        "limits": [
            session,
            {"kind": "weekly_all", "group": "weekly", "percent": overall, "resets_at": WEEK_RESET, "scope": None},
            {
                "kind": "weekly_scoped",
                "group": "weekly",
                "percent": fable,
                "resets_at": WEEK_RESET,
                "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
                "is_active": False,
            },
        ],
    }


def weekly_used(p):
    return [w.used for w in p.windows if w.name == "weekly"]


# Overall weekly over pace (65% used at 50% elapsed), Fable well under (28%).
HELD = payload(65, 28)

# --- off by default: the overall weekly gates and the scoped cap is ignored ----------------------
p = q._claude_from_payload(HELD)
check("unset: weekly reads the overall figure", weekly_used(p), [65.0])
check("unset: over-pace overall weekly holds claude", p.available, False)

# --- scope named: the scoped cap is the weekly window ---------------------------------------------
os.environ["EPSILONERIDANI_CLAUDE_WEEKLY_SCOPE"] = "Fable"
p = q._claude_from_payload(HELD)
check("Fable: weekly reads the scoped figure", weekly_used(p), [28.0])
check("Fable: the overall weekly no longer holds this worker", p.available, True)

os.environ["EPSILONERIDANI_CLAUDE_WEEKLY_SCOPE"] = "  fable "
check("matched case-insensitively, whitespace ignored", weekly_used(q._claude_from_payload(HELD)), [28.0])

by_id = payload(65, 28)
by_id["limits"][2]["scope"]["model"] = {"id": "claude-fable-5-1", "display_name": None}
os.environ["EPSILONERIDANI_CLAUDE_WEEKLY_SCOPE"] = "claude-fable-5-1"
check("matched by model id too", weekly_used(q._claude_from_payload(by_id)), [28.0])

os.environ["EPSILONERIDANI_CLAUDE_WEEKLY_SCOPE"] = "Fable"
p = q._claude_from_payload(payload(10, 70))
check("Fable: an over-pace scoped cap holds, however low the overall", (weekly_used(p), p.available), ([70.0], False))

over_session = dict(SESSION, percent=90)  # 20% of the session elapsed, 90% used
p = q._claude_from_payload(payload(10, 10, session=over_session))
check("Fable: an over-pace session still holds", p.available, False)

# --- a named scope the payload does not carry: fail closed, no flat fallback -----------------------
os.environ["EPSILONERIDANI_CLAUDE_WEEKLY_SCOPE"] = "Nonexistent"
p = q._claude_from_payload(payload(10, 10))
check(
    "unknown scope: weekly is absent, not the flat seven_day",
    [w.status for w in p.windows if w.name == "weekly"],
    ["absent"],
)
check("unknown scope: claude is held", p.available, False)

no_limits = payload(10, 10)
del no_limits["limits"]
os.environ["EPSILONERIDANI_CLAUDE_WEEKLY_SCOPE"] = "Fable"
p = q._claude_from_payload(no_limits)
check(
    "no limits array: scope cannot be read, held",
    ([w.status for w in p.windows if w.name == "weekly"], p.available),
    (["absent"], False),
)

# --- the bootstrap opens the window the worker will spend ------------------------------------------
os.environ.pop("EPSILONERIDANI_AUTHORING_CLAUDE_MODEL", None)
argv, _, _ = q._bootstrap_spec()
check("bootstrap without an authoring model: a bare `claude -p` turn", (argv[-2], len(argv)), ("-p", 3))
os.environ["EPSILONERIDANI_AUTHORING_CLAUDE_MODEL"] = "claude-fable-5-1"
argv, _, _ = q._bootstrap_spec()
check("bootstrap with an authoring model runs on it", argv[-2:], ["--model", "claude-fable-5-1"])

for var in ("EPSILONERIDANI_CLAUDE_WEEKLY_SCOPE", "EPSILONERIDANI_AUTHORING_CLAUDE_MODEL", "EPSILONERIDANI_PACE"):
    os.environ.pop(var, None)

print(f"\n{'PASS' if fails == 0 else f'FAIL ({fails})'}")
sys.exit(1 if fails else 0)
