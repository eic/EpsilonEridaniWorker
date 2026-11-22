#!/usr/bin/env python3
"""A window the spend journal shows exhausted holds the worker; a review leaves no workspace behind.

On 2026-11-22 three workers on one Claude account started reviews together from a 64% session reading
while the usage endpoint answered 429. The window ran out within five minutes, every review aborted
(engine exit 3), and the loops launched 12 more reviews over 20 minutes on the same cached reading, each
aborting at its first rubric, although the spend journal already said 101%. Every one of those runs also
left its 0.4-0.8 GB `epsiloneridani-review-<pr>-*` workspace in /tmp, and 255 of them filled bison's
root filesystem. Pinned:

  1. A permissive verdict (fresh cache, under pace) yields to a fresh journal reading that shows a gated
     window exhausted: no launch, the window reported exhausted.
  2. It never grants: a fresh journal reading under pace, or an exhausted one older than the stale gate,
     or one whose reset has passed, leaves the endpoint's verdict as it was.
  3. The host review hands the engine a fixed per-worker --workdir, empties it before the run (a killed
     earlier run's leftovers), and removes it afterwards, also when the engine aborts (exit 3).

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import types
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import epsiloneridani_worker as tc  # noqa: E402

quota = tc.quota
fails = 0


def check(name, got, expect):
    global fails
    ok = got == expect
    print(f"[{'OK ' if ok else 'XX '}] {name}: {got!r}")
    if not ok:
        print(f"      expected: {expect!r}")
        fails += 1


ENV_KEYS = (
    "CLAUDE_CONFIG_DIR",
    "EPSILONERIDANI_CLAUDE_ACCOUNTS_DIR",
    "EPSILONERIDANI_CLAUDE_WEEKLY_SCOPE",
    "EPSILONERIDANI_CLAUDE_NO_WEEKLY_CAP",
    "EPSILONERIDANI_CLAUDE_ALLOW_CREDITS",
)
root = Path(tempfile.mkdtemp(prefix="epsiloneridani-journal-exhausted-"))
saved_env = {k: os.environ.get(k) for k in ENV_KEYS}
saved_http = quota._http_get_json
saved_tempdir = tempfile.tempdir
try:
    for k in ENV_KEYS:
        os.environ.pop(k, None)
    os.environ["EPSILONERIDANI_CLAUDE_ACCOUNTS_DIR"] = str(root / "accounts")

    src = root / "logins" / "worker01"
    src.mkdir(parents=True)
    (src / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "acct-01"}}))
    home = root / "homes" / "worker01"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".epsiloneridani-creds-source").write_text(str(src))
    q = quota.Quota(SimpleNamespace(home=home, quota_cache=home / "cache"))
    journal = quota.claude_spend_journal(home)

    now = time.time()
    # Windows opened 2.5 h (session) and 3.5 days (weekly) ago: elapsed 50% each.
    five_reset = now + 2.5 * 3600
    week_reset = now + 3.5 * 86400

    def iso(ts):
        return datetime.fromtimestamp(ts, UTC).isoformat()

    def payload(five, week):
        return {
            "five_hour": {"utilization": five, "resets_at": iso(five_reset)},
            "seven_day": {"utilization": week, "resets_at": iso(week_reset)},
        }

    def line(seen, five, week, *, five_resets=five_reset):
        return {
            "ts": seen + 30,
            "label": "review",
            "rc": 0,
            "spend": None,
            "rate_limits": {
                "five_hour": {"status": "rejected", "utilization": five, "resetsAt": five_resets, "seen": seen},
                "seven_day": {"status": "allowed", "utilization": week, "resetsAt": week_reset, "seen": seen},
            },
        }

    def write_journal(*entries):
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text("".join(json.dumps(e) + "\n" for e in entries))

    def reset_state():
        q._forget_raw("claude")
        shared = q._shared_usage_paths()[0]
        for p in (journal, shared, shared.with_name(quota.CLAUDE_USAGE_TEMPLATE_FILE)):
            p.unlink(missing_ok=True)

    def permissive_reading():
        """A fresh, under-pace endpoint reading (10% session, 10% weekly), then the endpoint goes 429,
        as on 2026-11-22: the worker keeps answering from this reading."""
        quota._http_get_json = lambda *_a, **_k: (200, payload(10, 10), None)
        prov, _ = q._claude_pass("fp", "tok", refresh=True)
        quota._http_get_json = lambda *_a, **_k: (429, None, 3600)
        return prov

    def status(prov):
        return {w.name: w.status for w in prov.windows}

    print("1) fresh journal shows the session exhausted: hold")
    reset_state()
    check("the endpoint alone authorizes", permissive_reading().available, True)
    write_journal(line(now - 60, 1.01, 0.42))
    prov, _ = q._claude_pass("fp", "tok", refresh=False)
    check("launch withheld", prov.available, False)
    check("...the session reported exhausted", status(prov).get("session"), quota.STATUS_EXHAUSTED)
    check("...no bootstrap offered", prov.bootstrap_eligible, False)

    print("2) it never grants")
    reset_state()
    permissive_reading()
    write_journal(line(now - 60, 0.30, 0.20))
    prov, _ = q._claude_pass("fp", "tok", refresh=False)
    check("a journal under pace leaves the verdict", prov.available, True)

    reset_state()
    permissive_reading()
    write_journal(line(now - quota.CLAUDE_STALE_GATE_S - 120, 1.01, 0.42))
    prov, _ = q._claude_pass("fp", "tok", refresh=False)
    check("an exhausted reading older than the gate is ignored", prov.available, True)

    reset_state()
    permissive_reading()
    write_journal(line(now - 60, 1.01, 0.42, five_resets=now - 30))
    prov, _ = q._claude_pass("fp", "tok", refresh=False)
    check("an exhausted reading from a window since reset is ignored", prov.available, True)

    reset_state()
    quota._http_get_json = lambda *_a, **_k: (200, payload(80, 10), None)
    over, _ = q._claude_pass("fp", "tok", refresh=True)
    write_journal(line(now - 60, 0.30, 0.20))
    prov, _ = q._claude_pass("fp", "tok", refresh=False)
    check("an over-pace verdict is not loosened by a lower journal", (over.available, prov.available), (False, False))

    print("3) the review workspace")
    tempfile.tempdir = str(root / "tmp")
    Path(tempfile.tempdir).mkdir()

    class Counters:
        def __init__(self):
            self.v = {}

        def read(self, k):
            return self.v.get(k, 0)

        def incr(self, k):
            self.v[k] = self.v.get(k, 0) + 1
            return self.v[k]

        def write(self, k, n):
            self.v[k] = n

    state = root / "state"
    (state / "store").mkdir(parents=True)
    w = types.SimpleNamespace(
        cfg=types.SimpleNamespace(state=state, logdir=state, wid="worker01", store_dir=state / "store"),
        counters=Counters(),
        rs=types.SimpleNamespace(bust=lambda pr: None, review_rounds=lambda pr, c: 0),
        gh=types.SimpleNamespace(add_reaction=lambda i: True, remove_reaction=lambda i: True),
    )
    workdir = tc.work_units._review_workdir(w)
    check(
        "one fixed path per worker, under the temp dir",
        workdir,
        Path(tempfile.tempdir) / "epsiloneridani-review-worker01",
    )

    seen = {}

    def engine(rc):
        def run(argv, *_a, **_k):
            wd = Path(argv[argv.index("--workdir") + 1])
            seen["workdir"] = wd
            seen["leftover"] = (wd / "stale").exists()
            (wd / "code").mkdir(parents=True)  # what the engine would clone, before aborting or finishing
            (wd / "code" / "big").write_text("x")
            return rc

        return run

    def drive(rc):
        c = types.SimpleNamespace(pr=42, head="a" * 40, contest=None, contest_reply_id=None)
        orig = tc.work_units.run_to_logfile, tc.work_units._sync_review_outbox, tc.work_units.me
        tc.work_units.run_to_logfile = engine(rc)
        tc.work_units._sync_review_outbox = lambda *a, **k: 0
        tc.work_units.me = lambda: "tester"
        try:
            tc.work_units.do_review(w, SimpleNamespace(), c, SimpleNamespace(work_model="claude"), bubble=False)
        except tc.config.NoProgress:
            pass
        finally:
            tc.work_units.run_to_logfile, tc.work_units._sync_review_outbox, tc.work_units.me = orig

    (workdir / "stale").mkdir(parents=True)  # a killed earlier run's leftovers
    drive(tc.constants.REVIEW_PROVIDER_DOWN_EXIT)
    check("the engine is handed the worker's workdir", seen.get("workdir"), workdir)
    check("...emptied of an earlier run's leftovers first", seen.get("leftover"), False)
    check("...and removed after an aborted run", workdir.exists(), False)
    drive(0)
    check("...and after a completed one", workdir.exists(), False)
finally:
    quota._http_get_json = saved_http
    tempfile.tempdir = saved_tempdir
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(root, ignore_errors=True)

print("PASS" if not fails else f"FAIL ({fails})")
sys.exit(1 if fails else 0)
