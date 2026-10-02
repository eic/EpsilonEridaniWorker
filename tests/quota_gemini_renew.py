#!/usr/bin/env python3
"""The pacer renews an expired Antigravity (Gemini) access token by running `agy models`.

The token lasts an hour and only `agy` renews it, as a side effect of running. A worker paced for
longer than that used to find it expired, read HTTP 401 from the usage endpoint, and stay
unavailable — the rounds that would have renewed it never started. Properties under test:

  - the RFC 3339 expiry agy writes (nine fractional digits, numeric offset) parses;
  - a token past its stored expiry is renewed BEFORE the usage read, which then uses the new token;
  - a token whose stored expiry looks fine but which the endpoint rejects (401) is renewed once and
    the read retried with the new token;
  - `agy` runs with the quota cache as its working directory, never the operator's home;
  - attempts are rate-limited (GEMINI_RENEW_RETRY_S): a second 401 inside the interval runs no `agy`;
  - an `agy` that leaves the token unchanged reports gemini unavailable with the "could not be
    renewed" error, which the loop still recognises as a credential problem;
  - $EPSILONERIDANI_GEMINI_NO_RENEW=1 turns renewal off entirely;
  - only a caller about to act renews (renew=True, as the loop passes through Quota.choose): an
    inspection read like `epsiloneridani status` reports the expired token and runs nothing.

Dependency-free; no network. `agy` is a stub script on PATH.
"""

import json
import os
import shutil
import sys
import tempfile
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import epsiloneridani_worker as tc

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")
    fails += not ok


os.environ["EPSILONERIDANI_PACE"] = "0:0,100:100"
os.environ.pop("EPSILONERIDANI_GEMINI_NO_RENEW", None)
os.environ.pop("GEMINI_API_KEY", None)

# --- 1. expiry parsing ----------------------------------------------------------------------------
check(
    "nanosecond RFC 3339 with offset parses",
    tc.quota._parse_rfc3339("2026-10-02T10:00:53.766296895-05:00"),
    datetime(2026, 10, 2, 15, 0, 53, 766296, tzinfo=UTC).timestamp(),
)
check(
    "Z suffix parses",
    tc.quota._parse_rfc3339("2026-10-02T15:00:00Z"),
    datetime(2026, 10, 2, 15, tzinfo=UTC).timestamp(),
)
check(
    "no fraction parses",
    tc.quota._parse_rfc3339("2026-10-02T15:00:00+00:00"),
    datetime(2026, 10, 2, 15, tzinfo=UTC).timestamp(),
)
check("naive time is not trusted", tc.quota._parse_rfc3339("2026-10-02T15:00:00"), None)
check("garbage is None", tc.quota._parse_rfc3339("soon"), None)
check("non-string is None", tc.quota._parse_rfc3339(None), None)

# --- fixtures -------------------------------------------------------------------------------------
tmp = Path(tempfile.mkdtemp(prefix="gemini-renew-"))
gdir = tmp / "gemini"
tok_file = gdir / "antigravity-cli" / "antigravity-oauth-token"
tok_file.parent.mkdir(parents=True)
cache = tmp / "cache"
bindir = tmp / "bin"
bindir.mkdir()
calls = tmp / "agy-calls"
os.environ["GEMINI_CONFIG_DIR"] = str(gdir)
os.environ["PATH"] = f"{bindir}:{os.environ['PATH']}"


def rfc3339(delta_s: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=delta_s)).astimezone().isoformat()


def write_token(access: str, expires_in: float) -> None:
    tok_file.write_text(
        json.dumps(
            {
                "auth_method": "consumer",
                "id_token": "id",
                "token": {
                    "access_token": access,
                    "expiry": rfc3339(expires_in),
                    "refresh_token": "reusable",
                    "token_type": "Bearer",
                },
            }
        )
    )


def install_agy(new_access: str | None, rc: int = 0) -> None:
    """A stub `agy` that logs its argv and cwd and, if new_access is given, rewrites the token as
    the real CLI does when it renews."""
    script = bindir / "agy"
    rewrite = ""
    if new_access:
        body = json.dumps(
            {
                "auth_method": "consumer",
                "id_token": "id",
                "token": {
                    "access_token": new_access,
                    "expiry": rfc3339(3600),
                    "refresh_token": "reusable",
                    "token_type": "Bearer",
                },
            }
        )
        rewrite = f"cat > '{tok_file}' <<'EOF'\n{body}\nEOF\n"
    script.write_text(f"#!/bin/sh\necho \"$* @ $(pwd)\" >> '{calls}'\n{rewrite}exit {rc}\n")
    script.chmod(0o755)


def agy_calls() -> list[str]:
    return calls.read_text().splitlines() if calls.exists() else []


def reset() -> None:
    shutil.rmtree(cache, ignore_errors=True)
    calls.unlink(missing_ok=True)


PAYLOAD = {"models": {"gemini-3.1-pro-high": {"quotaInfo": {"remainingFraction": 0.9, "resetTime": rfc3339(4 * 3600)}}}}


def endpoint(valid: set[str]):
    """A usage endpoint that accepts only the given access tokens; records each bearer it saw."""
    seen: list[str] = []

    def fake(url, headers, data=b"{}", timeout=15):
        bearer = headers["Authorization"].removeprefix("Bearer ")
        seen.append(bearer)
        return (200, PAYLOAD, None) if bearer in valid else (401, {}, None)

    return seen, fake


def quota() -> "tc.Quota":
    return tc.Quota(types.SimpleNamespace(home=tmp, quota_cache=cache))


# --- 2. expired on disk: renewed before the read --------------------------------------------------
reset()
write_token("old", -60)
install_agy("new")
seen, fake = endpoint({"new"})
with patch.object(tc.quota, "_http_post_json", side_effect=fake):
    p = quota().gemini(renew=True)
check("expired token: gemini available after renewal", p.available, True)
check("expired token: one agy run", len(agy_calls()), 1)
check("expired token: agy ran `models` in the quota cache", agy_calls()[:1], [f"models @ {cache}"])
check("expired token: the read used only the new token", seen, ["new"])

# --- 3. stored expiry fine, endpoint says 401: renew once, retry ----------------------------------
reset()
write_token("revoked", 3000)
install_agy("fresh")
seen, fake = endpoint({"fresh"})
with patch.object(tc.quota, "_http_post_json", side_effect=fake):
    p = quota().gemini(renew=True)
check("401: gemini available after renew-and-retry", p.available, True)
check("401: one agy run", len(agy_calls()), 1)
check("401: old token then new token", seen, ["revoked", "fresh"])

# --- 4. rate limit: a second 401 inside the interval does not run agy again -----------------------
reset()
write_token("dead", 3000)
install_agy(None, rc=1)  # agy cannot renew this login
seen, fake = endpoint(set())
with patch.object(tc.quota, "_http_post_json", side_effect=fake):
    p1 = quota().gemini(refresh=True, renew=True)
    p2 = quota().gemini(refresh=True, renew=True)
check("unrenewable: gemini unavailable", p1.available, False)
check(
    "unrenewable: error says it could not be renewed",
    p1.error,
    "gemini token expired and could not be renewed; log in to agy again",
)
check("unrenewable: agy ran once across two polls", len(agy_calls()), 1)
check("unrenewable: no retry without a new token", seen, ["dead", "dead"])
check("unrenewable: second poll unavailable too", p2.available, False)
check(
    "unrenewable: the loop still recognises it as a credential problem",
    tc.loop._credential_hint("gemini", p1),
    ". Run `agy` to authenticate or set GEMINI_API_KEY",
)

# --- 5. opt-out ----------------------------------------------------------------------------------
reset()
write_token("old", -60)
install_agy("new")
seen, fake = endpoint({"new"})
os.environ["EPSILONERIDANI_GEMINI_NO_RENEW"] = "1"
try:
    with patch.object(tc.quota, "_http_post_json", side_effect=fake):
        p = quota().gemini(renew=True)
finally:
    os.environ.pop("EPSILONERIDANI_GEMINI_NO_RENEW", None)
check("opt-out: no agy run", agy_calls(), [])
check("opt-out: gemini unavailable on the expired token", p.available, False)

# --- 6. an inspection read (renew=False, as `status` does) never runs agy -------------------------
reset()
write_token("old", -60)
install_agy("new")
seen, fake = endpoint({"new"})
with patch.object(tc.quota, "_http_post_json", side_effect=fake):
    p = quota().gemini()
check("inspection: no agy run", agy_calls(), [])
check("inspection: reports the expired token", p.available, False)
check(
    "inspection: does not claim a renewal it never tried",
    p.error,
    "gemini token expired; refresh left to the operator",
)

# --- 6b. choose(renew=True) — the loop's path — reaches the renewal --------------------------------
reset()
write_token("old", -60)
install_agy("new")
seen, fake = endpoint({"new"})
with patch.object(tc.quota, "_http_post_json", side_effect=fake):
    model, snap = quota().choose("gemini", refresh=True, renew=True)
check("choose(renew=True): gemini chosen after renewal", model is not None, True)
check("choose(renew=True): one agy run", len(agy_calls()), 1)

# --- 7. a valid token is left alone ---------------------------------------------------------------
reset()
write_token("good", 3000)
install_agy("unexpected")
seen, fake = endpoint({"good"})
with patch.object(tc.quota, "_http_post_json", side_effect=fake):
    p = quota().gemini(renew=True)
check("valid token: available", p.available, True)
check("valid token: no agy run", agy_calls(), [])

shutil.rmtree(tmp, ignore_errors=True)
print()
if fails:
    print(f"FAIL: {fails} mismatch(es)")
    sys.exit(1)
print("all gemini renewal checks passed")
