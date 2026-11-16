#!/usr/bin/env python3
"""A fresh spend-journal reading stands in for a usage endpoint that will not answer.

The usage endpoint answers most polls with 429, and a cached reading older than CLAUDE_STALE_GATE_S may
not authorize a launch, so on 2026-11-14/15 the single-account workers sat idle about half the time with
quota to spare. Every Claude run already journals the utilization its rate-limit headers reported
(spend.jsonl), which is as current as the endpoint would be. Pinned:

  1. 429 with no usable cache, and a fresh journal reading under pace: the worker may launch, paced as
     of the moment the reading was seen.
  2. A stale cached reading (past the gate) and a fresh journal reading: the journal decides.
  3. The journal is held to the endpoint's rules: a reading older than the gate, one whose reset has
     passed, one missing a gated window, or one over pace does not authorize a launch.
  4. The newest reading per window wins, whatever order the lines were appended in, and the pair is
     dated to the older of the two.
  5. It fails closed without a real payload to use as a template (credits status unknown), keeps the
     template's credit guard, and never reads a model-scoped weekly cap from the headers.
  6. A verdict the endpoint actually gave (a 401, an over-pace reading) is not overridden.
  7. Host review rounds hand the engine the journal's path.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import json
import os
import shutil
import sys
import tempfile
import time
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
root = Path(tempfile.mkdtemp(prefix="epsiloneridani-journal-reading-"))
saved_env = {k: os.environ.get(k) for k in ENV_KEYS}
saved_http = quota._http_get_json
try:
    for k in ENV_KEYS:
        os.environ.pop(k, None)
    os.environ["EPSILONERIDANI_CLAUDE_ACCOUNTS_DIR"] = str(root / "accounts")

    src = root / "logins" / "worker27"
    src.mkdir(parents=True)
    (src / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "acct-27"}}))
    home = root / "homes" / "worker27"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".epsiloneridani-creds-source").write_text(str(src))
    q = quota.Quota(SimpleNamespace(home=home, quota_cache=home / "cache"))
    journal = quota.claude_spend_journal(home)
    check("the journal is the account's", journal.parent, q._claude_shared_dir())

    now = time.time()
    # Windows opened 2.5 h (session) and 3.5 days (weekly) ago: elapsed 50% each.
    five_reset = now + 2.5 * 3600
    week_reset = now + 3.5 * 86400

    def iso(ts):
        return datetime.fromtimestamp(ts, UTC).isoformat()

    def payload(five, week, *, credits=False):
        p = {
            "five_hour": {"utilization": five, "resets_at": iso(five_reset)},
            "seven_day": {"utilization": week, "resets_at": iso(week_reset)},
        }
        if credits:
            p["extra_usage"] = {"is_enabled": True}
        return p

    def line(seen, five=None, week=None, *, five_resets=five_reset, week_resets=week_reset):
        limits = {}
        if five is not None:
            limits["five_hour"] = {"status": "allowed", "utilization": five, "resetsAt": five_resets, "seen": seen}
        if week is not None:
            limits["seven_day"] = {"status": "allowed", "utilization": week, "resetsAt": week_resets, "seen": seen}
        return {"ts": seen + 30, "label": "agent-claude", "rc": 0, "spend": None, "rate_limits": limits or None}

    def write_journal(*entries):
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text("".join(json.dumps(e) + "\n" for e in entries))

    def reset_state():
        q._forget_raw("claude")
        shared = q._shared_usage_paths()[0]
        for p in (journal, shared, shared.with_name(quota.CLAUDE_USAGE_TEMPLATE_FILE)):
            p.unlink(missing_ok=True)

    answer = {"value": (429, None, 3600)}
    quota._http_get_json = lambda *_a, **_k: answer["value"]

    def seed_template(five=10, week=10, *, at=None, credits=False):
        """A real reading as the endpoint once gave it: the template the journal fills in."""
        answer["value"] = (200, payload(five, week, credits=credits), None)
        q._claude_pass("fp", "tok", refresh=True)
        if at is not None:
            entry = json.loads((q.cache_dir / "quota-claude.json").read_text())
            entry["fetched_at"] = at
            (q.cache_dir / "quota-claude.json").write_text(json.dumps(entry))
            shared = q._shared_usage_paths()[0]
            data = json.loads(shared.read_text())
            data["fetched_at"] = at
            shared.write_text(json.dumps(data))
        answer["value"] = (429, None, 3600)
        q._shared_usage_record("fp", blocked_until=0)  # clear the shared back-off, keep the reading

    def windows(prov):
        return {w.name: (round(w.used or 0), w.status) for w in prov.windows}

    print("1) 429, no usable cache, fresh journal under pace")
    reset_state()
    seed_template(at=now - 2 * 3600)  # older than the gate: cannot authorize on its own
    write_journal(line(now - 120, 0.20, 0.30))
    prov, _ = q._claude_pass("fp", "tok", refresh=True)
    check("launch authorized", prov.available, True)
    check("...on the journal's figures", windows(prov).get("session", (None,))[0], 20)
    check("...weekly too", windows(prov).get("weekly", (None,))[0], 30)

    print("2) stale cache and fresh journal: the journal decides")
    reset_state()
    seed_template(five=5, week=5, at=now - 40 * 60)
    stale, _ = q._claude_endpoint_pass("fp", "tok", refresh=False)
    check("the cache alone is stale-gated", "could not be refreshed" in (stale.error or ""), True)
    write_journal(line(now - 60, 0.25, 0.28))
    prov, _ = q._claude_pass("fp", "tok", refresh=False)
    check("the journal authorizes", prov.available, True)
    check("...with its own (higher) figure", windows(prov)["session"][0], 25)

    print("3) the journal is held to the endpoint's rules")
    for name, entries in (
        ("older than the gate", [line(now - quota.CLAUDE_STALE_GATE_S - 60, 0.2, 0.3)]),
        ("reset already passed", [line(now - 60, 0.2, 0.3, five_resets=now - 10)]),
        ("weekly missing", [line(now - 60, 0.2, None)]),
        ("over pace", [line(now - 60, 0.95, 0.3)]),
        ("from the future", [line(now + 600, 0.2, 0.3)]),
        ("malformed", [{"rate_limits": {"five_hour": {"utilization": "x", "resetsAt": five_reset, "seen": now}}}]),
        ("an int too large for a float", [line(now - 60, 0.2, 0.3, five_resets=10**1000)]),
    ):
        reset_state()
        seed_template(at=now - 2 * 3600)
        write_journal(*entries)
        prov, _ = q._claude_pass("fp", "tok", refresh=True)
        check(f"{name}: no launch", prov.available, False)

    print("4) newest per window, dated to the older")
    reset_state()
    seed_template(at=now - 2 * 3600)
    write_journal(
        line(now - 60, 0.30, None),  # newest session, appended first
        line(now - 600, 0.10, 0.20),  # older pair, appended later
        line(now - 300, None, 0.40),  # newest weekly
    )
    found = quota._journal_windows(journal, time.time(), quota.CLAUDE_STALE_GATE_S)
    check("session from the newest line", round(found[0]["five_hour"][0]), 30)
    check("weekly from the newest line", round(found[0]["seven_day"][0]), 40)
    check("dated to the older of the two", round(found[1]), round(now - 300))

    print("5) template, credits and scope")
    reset_state()
    write_journal(line(now - 60, 0.2, 0.3))
    prov, _ = q._claude_pass("fp", "tok", refresh=True)
    check("no template: fails closed", prov.available, False)
    check("...still reporting the 429", (prov.error or "").startswith("claude usage HTTP 429"), True)

    filled = quota._journal_payload(
        {**payload(1, 1, credits=True), "limits": [{"group": "session", "percent": 1}]},
        {"five_hour": (42.0, five_reset), "seven_day": (7.0, week_reset)},
    )
    check("the template's credits status is kept (for the credit guard)", quota._extra_usage_enabled(filled), True)
    check("...its `limits` array dropped (it would outrank the journal)", "limits" in filled, False)
    check("...and the journal's figures filled in", filled["five_hour"]["utilization"], 42.0)

    reset_state()
    seed_template(at=now - 2 * 3600)
    write_journal(line(now - 60, 0.2, 0.3))
    os.environ["EPSILONERIDANI_CLAUDE_WEEKLY_SCOPE"] = "Fable"
    check("a model-scoped weekly never reads the headers", quota._journal_windows(journal, time.time(), 1800), None)
    os.environ.pop("EPSILONERIDANI_CLAUDE_WEEKLY_SCOPE")
    os.environ["EPSILONERIDANI_CLAUDE_NO_WEEKLY_CAP"] = "1"
    write_journal(line(now - 60, 0.2, None))
    found = quota._journal_windows(journal, time.time(), 1800)
    check("without a weekly cap, the session alone suffices", found is not None and list(found[0]), ["five_hour"])
    os.environ.pop("EPSILONERIDANI_CLAUDE_NO_WEEKLY_CAP")

    print("5b) the template survives a bootstrap's cache wipe")
    reset_state()
    seed_template(at=now - 2 * 3600)
    q._forget_raw("claude")  # what a bootstrap request does to the own and shared readings
    q._shared_usage_record("fp", blocked_until=time.time() + 3600)  # then the next fetch meets a 429
    write_journal(line(now - 60, 0.2, 0.3))
    prov, _ = q._claude_pass("fp", "tok", refresh=True)
    check("the kept template still lets the journal decide", prov.available, True)
    template_file = q._shared_usage_paths()[0].with_name(quota.CLAUDE_USAGE_TEMPLATE_FILE)
    data = json.loads(template_file.read_text())
    data["account"] = "account:someone-else"
    template_file.write_text(json.dumps(data))
    q._forget_raw("claude")
    q._shared_usage_record("fp", blocked_until=time.time() + 3600)
    prov, _ = q._claude_pass("fp", "tok", refresh=True)
    check("...but never another account's", prov.available, False)
    template_file.unlink()

    print("6) a verdict the endpoint gave stands")
    reset_state()
    seed_template(at=now - 2 * 3600)
    write_journal(line(now - 60, 0.2, 0.3))
    answer["value"] = (401, None, None)
    prov, _ = q._claude_pass("fp", "tok", refresh=True)
    check(
        "a 401 is not overridden",
        (prov.available, (prov.error or "").startswith("claude usage HTTP 401")),
        (False, True),
    )
    answer["value"] = (200, payload(95, 30), None)
    prov, _ = q._claude_pass("fp", "tok", refresh=True)
    check("a fresh over-pace reading is not overridden", prov.available, False)
    answer["value"] = (429, None, 3600)

    print("7) host review rounds name the journal")
    env = tc.work_units._review_engine_env(SimpleNamespace(cfg=SimpleNamespace(home=home)))
    check("the engine is told the journal's path", env.get(quota.CLAUDE_SPEND_JOURNAL_ENV), str(journal))
    check("...on top of the worker's own environment", env.get("PATH"), os.environ.get("PATH"))
    check("no home: inherit unchanged", tc.work_units._review_engine_env(SimpleNamespace(cfg=SimpleNamespace())), None)
finally:
    quota._http_get_json = saved_http
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(root, ignore_errors=True)

print(f"\n{fails} failure(s)")
sys.exit(1 if fails else 0)
