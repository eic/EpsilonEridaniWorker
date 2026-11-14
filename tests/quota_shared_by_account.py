#!/usr/bin/env python3
"""Workers on separate logins for one Claude account share one usage reading.

Each worker on its own login (claude_config_dir) used to keep its quota state beside that login, so
five workers logged in separately to one account each polled the usage endpoint, each met its 429s on
its own, and each could spend its own bootstrap request. A login names its account in Claude Code's
.claude.json (`oauthAccount.accountUuid`); when it does, the shared state lives in a per-account
directory. Pinned:

  1. Two logins for one account, with different tokens, share one directory: the second worker reuses
     the first's reading instead of fetching, and waits out the first's 429.
  2. A login for another account shares nothing with them.
  3. The bootstrap reservation is per account too: a second login for the account cannot reserve an
     episode the first holds.
  4. A login that does not name its account keeps its state beside the credential source, as before,
     and matches by token fingerprint. So does the default ~/.claude, whose .claude.json sits beside it.
  5. An entry written before accounts were recorded is not served to an account-keyed worker.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

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

quota = tc.quota
fails = 0


def check(name, got, expect):
    global fails
    ok = got == expect
    print(f"[{'OK ' if ok else 'XX '}] {name}: {got!r}")
    if not ok:
        print(f"      expected: {expect!r}")
        fails += 1


root = Path(tempfile.mkdtemp(prefix="epsiloneridani-shared-account-"))
saved_env = {k: os.environ.get(k) for k in ("CLAUDE_CONFIG_DIR", "EPSILONERIDANI_CLAUDE_ACCOUNTS_DIR")}
saved_http = quota._http_get_json
try:
    os.environ.pop("CLAUDE_CONFIG_DIR", None)
    os.environ["EPSILONERIDANI_CLAUDE_ACCOUNTS_DIR"] = str(root / "accounts")

    def login(name: str, account: str | None) -> Path:
        """A login directory as `workers login` leaves it (claude_config_dir)."""
        d = root / "logins" / name
        d.mkdir(parents=True)
        if account is not None:
            (d / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": account}}))
        return d

    def worker(name: str, src: Path):
        """A worker as isolate_home leaves it: its own home and cache, a marker naming its login."""
        home = root / "homes" / name
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / ".epsiloneridani-creds-source").write_text(str(src))
        return quota.Quota(SimpleNamespace(home=home, quota_cache=home / "cache"))

    payload = {
        "five_hour": {"utilization": 10, "resets_at": (datetime.now(UTC) + timedelta(hours=4)).isoformat()},
        "seven_day": {"utilization": 10, "resets_at": (datetime.now(UTC) + timedelta(days=6)).isoformat()},
    }
    calls = []
    answer = {"value": (200, payload, None)}
    quota._http_get_json = lambda *_a, **_k: calls.append("fetch") or answer["value"]

    w01 = worker("worker01", login("worker01", "acct-shared"))
    w03 = worker("worker03", login("worker03", "acct-shared"))
    w27 = worker("worker27", login("worker27", "acct-own"))

    print("1) two logins for one account")
    check("they share one directory", w01._claude_shared_dir() == w03._claude_shared_dir(), True)
    check("...under the accounts root", w01._claude_shared_dir().parent == root / "accounts", True)
    w01._claude_pass("fp-01", "token-01", refresh=True)
    check("the first fetches", len(calls), 1)
    _prov, readings = w03._claude_pass("fp-03", "token-03", refresh=True)
    check("the second reuses its reading, despite another token", len(calls), 1)
    check("...with real readings", bool(readings), True)

    def forget():
        w01._forget_raw("claude")
        w03._forget_raw("claude")
        w27._forget_raw("claude")

    forget()
    answer["value"] = (429, None, 900)
    w01._claude_pass("fp-01", "token-01", refresh=True)
    check("a 429 is fetched once", len(calls), 2)
    held, _ = w03._claude_pass("fp-03", "token-03", refresh=True)
    check("the other login waits it out without asking", len(calls), 2)
    check("...and says why", "sibling worker's Retry-After" in (held.error or ""), True)

    print("2) another account")
    check("has its own directory", w27._claude_shared_dir() != w01._claude_shared_dir(), True)
    answer["value"] = (200, payload, None)
    w27._claude_pass("fp-27", "token-27", refresh=True)
    check("is not held by the other account's 429, and fetches", len(calls), 3)

    print("3) the bootstrap reservation")
    check("is keyed by the account", w01._claude_account_key(), "account:acct-shared")
    held_01, _ = w01._reserve_bootstrap(["episode"])
    held_03, why = w03._reserve_bootstrap(["episode"])
    check("the first login reserves the episode", held_01, True)
    check("a second login for the account cannot", (held_03, "another worker" in why), (False, True))
    held_27, _ = w27._reserve_bootstrap(["episode"])
    check("another account can", held_27, True)

    print("4) a login that names no account")
    anon_src = login("anon", None)
    a1, a2 = worker("anon-1", anon_src), worker("anon-2", anon_src)
    check(
        "keeps its state beside the credential source", a1._claude_shared_dir(), anon_src / quota.CLAUDE_QUOTA_DIRNAME
    )
    forget()
    a1._claude_pass("fp-anon", "token", refresh=True)
    a2._claude_pass("fp-anon", "token", refresh=True)
    check("workers mirroring it still share by token", len(calls), 4)
    a2._claude_pass("fp-other", "token-2", refresh=True)
    check("...and another token is not served it", len(calls), 5)
    default_home = root / "operator"
    (default_home / ".claude").mkdir(parents=True)
    (default_home / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "acct-shared"}}))
    op = quota.Quota(SimpleNamespace(home=default_home, quota_cache=default_home / "cache"))
    check("the default ~/.claude finds ~/.claude.json", op._claude_shared_dir() == w01._claude_shared_dir(), True)

    os.environ["EPSILONERIDANI_CLAUDE_ACCOUNTS_DIR"] = " ~/quota-accounts "
    check("a ~ in the override is expanded", quota.claude_accounts_root(), Path.home() / "quota-accounts")
    os.environ["EPSILONERIDANI_CLAUDE_ACCOUNTS_DIR"] = "  "
    check(
        "a blank override falls back to the default", quota.claude_accounts_root().name, quota.CLAUDE_ACCOUNTS_DIRNAME
    )
    os.environ["EPSILONERIDANI_CLAUDE_ACCOUNTS_DIR"] = str(root / "accounts")

    print("5) an entry from before accounts were recorded")
    shared = w01._shared_usage_paths()[0]
    shared.write_text(json.dumps({"fp": "fp-01", "fetched_at": time.time(), "payload": payload}))
    w01._claude_pass("fp-01", "token-01", refresh=True)
    check("is not served, even to the token that wrote it", len(calls), 6)
    check(
        "...and is replaced by an account-keyed one",
        json.loads(shared.read_text()).get("account"),
        "account:acct-shared",
    )
finally:
    quota._http_get_json = saved_http
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(root, ignore_errors=True)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
