#!/usr/bin/env python3
"""Workers measuring the same Claude credential source share one usage reading and one back-off.

Each worker used to poll the usage endpoint itself, so a fleet multiplied the calls for one answer, and
when the endpoint rate-limited (a 429 asking for up to an hour) every worker found out separately. Two
workers here have private caches but one credential source. A sibling's fresh reading is reused instead
of fetched, a stale one is not, a different token is not served it, a 429's Retry-After holds every
worker back until it passes, a bootstrap's invalidation reaches the shared reading, and an unusable
shared directory falls back to fetching (sharing fails open)."""

import json
import os
import shutil
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import epsiloneridani_worker as tc  # noqa: E402

fails = 0


def check(name, got, expect):
    global fails
    ok = got == expect
    print(f"[{'OK ' if ok else 'XX '}] {name}: {got!r}")
    if not ok:
        print(f"      expected: {expect!r}")
        fails += 1


os.environ.pop("CLAUDE_CONFIG_DIR", None)
root = Path(tempfile.mkdtemp(prefix="epsiloneridani-shared-usage-"))
saved_http = tc.quota._http_get_json
try:
    source = root / "operator" / ".claude"  # the login every worker mirrors
    source.mkdir(parents=True)

    def worker(name: str, src: Path = source):
        """A worker as isolate_home leaves it: its own home and cache, a marker naming the source."""
        home = root / name
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / ".epsiloneridani-creds-source").write_text(str(src))
        return tc.quota.Quota(SimpleNamespace(home=home, quota_cache=home / "cache"))

    a, b = worker("a"), worker("b")
    shared_file = source / tc.quota.CLAUDE_QUOTA_DIRNAME / tc.quota.CLAUDE_USAGE_SHARED_FILE
    payload = {
        "five_hour": {"utilization": 10, "resets_at": (datetime.now(UTC) + timedelta(hours=4)).isoformat()},
        "seven_day": {"utilization": 10, "resets_at": (datetime.now(UTC) + timedelta(days=6)).isoformat()},
    }
    calls = []
    answer = {"value": (200, payload, None)}
    tc.quota._http_get_json = lambda *_a, **_k: calls.append("fetch") or answer["value"]

    # --- a sibling's fresh reading -------------------------------------------------------------------
    first, _ = a._claude_pass("fp", "token", refresh=True)
    check("the first worker fetches", len(calls), 1)
    check("...and publishes its reading", json.loads(shared_file.read_text()).get("payload"), payload)
    second, readings = b._claude_pass("fp", "token", refresh=True)
    check("a sibling reuses it instead of fetching", len(calls), 1)
    check("...reaching the same verdict", (second.available, second.error), (first.available, first.error))
    check("...with real readings", bool(readings), True)

    entry = json.loads(shared_file.read_text())
    entry["fetched_at"] = time.time() - tc.quota.CLAUDE_USAGE_SHARE_S - 5
    shared_file.write_text(json.dumps(entry))
    b._claude_pass("fp", "token", refresh=True)
    check("a reading older than the sharing window is fetched afresh", len(calls), 2)
    b._claude_pass("other-fp", "other-token", refresh=True)
    check("a different token is not served the shared reading", len(calls), 3)

    # --- a sibling's 429 ------------------------------------------------------------------------------
    def forget_all():
        a._forget_raw("claude")  # also drops the shared reading
        b._forget_raw("claude")

    forget_all()
    answer["value"] = (429, {}, 600.0)
    n = len(calls)
    limited, _ = a._claude_pass("fp", "token", refresh=True)
    check("a 429 is fetched once", len(calls) - n, 1)
    check("...and reported as rate-limited", "rate-limited" in (limited.error or ""), True)
    held, _ = b._claude_pass("fp", "token", refresh=True)
    check("a sibling waits it out without asking", len(calls) - n, 1)
    check("...naming why", "sibling worker's Retry-After" in (held.error or ""), True)
    check("...for the rest of the Retry-After", 590 < (held.retry_after or 0) <= 600, True)
    entry = json.loads(shared_file.read_text())
    entry["blocked_until"] = time.time() - 1
    shared_file.write_text(json.dumps(entry))
    answer["value"] = (200, payload, None)
    b._claude_pass("fp", "token", refresh=True)
    check("once the Retry-After has passed, the endpoint is asked again", len(calls) - n, 2)

    # A worker answering a 429 from its own cache still warns the siblings, and a held-back sibling
    # with a valid reading of its own uses it, as it would after a 429 of its own.
    shared_file.unlink()
    answer["value"] = (429, {}, 600.0)
    n = len(calls)
    served, _ = b._claude_pass("fp", "token", refresh=True)
    check("a 429 answered from the private cache", (len(calls) - n, served.error), (1, None))
    check("...is still shared", (json.loads(shared_file.read_text()).get("blocked_until") or 0) > time.time(), True)
    a._claude_pass("fp", "token", refresh=True)  # a's private cache was emptied above
    a._store_raw("claude", payload, "fp", time.time() + 3600, time.time())
    own, _ = a._claude_pass("fp", "token", refresh=True)
    check("a held-back sibling answers from its own valid cache", (len(calls) - n, own.error), (1, None))

    forget_all()
    answer["value"] = (429, {}, None)
    n = len(calls)
    a._claude_pass("fp", "token", refresh=True)
    b._claude_pass("fp", "token", refresh=True)
    check("a 429 without Retry-After holds no one back", len(calls) - n, 2)
    answer["value"] = (200, payload, None)

    # --- invalidation and failure -----------------------------------------------------------------------
    a._claude_pass("fp", "token", refresh=True)
    check("a fresh reading is shared again", shared_file.exists(), True)
    a._forget_raw("claude")
    check("a bootstrap's invalidation drops the shared reading too", shared_file.exists(), False)

    blocker = root / "not-a-directory"
    blocker.write_text("")
    lonely = worker("c", blocker / ".claude")  # its shared directory cannot be created
    before = len(calls)
    unshared, _ = lonely._claude_pass("fp", "token", refresh=True)
    check("an unusable shared directory falls back to fetching", (len(calls) - before, unshared.error), (1, None))
    bare = tc.quota.Quota.__new__(tc.quota.Quota)  # no configuration: nothing to share through
    check("a pacer without a configuration does not share", bare._shared_usage_paths(), None)
finally:
    tc.quota._http_get_json = saved_http
    shutil.rmtree(root, ignore_errors=True)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
