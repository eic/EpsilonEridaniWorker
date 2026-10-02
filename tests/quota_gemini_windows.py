#!/usr/bin/env python3
"""The Gemini / Antigravity usage reader and pacer.

Gemini quota is queried via Antigravity backend (fetchAvailableModels) returning:
  "quotaInfo": {
      "remainingFraction": <float 0..1>,
      "resetTime": "<ISO8601>"
  }
per model (defaulting to `gemini-3.1-pro-high`).

The session window is a 5-hour rolling window (18000s). The pacer classifies:
  - under-pace: used% < budget% -> available
  - at-budget:  used% == budget% -> unavailable (soft block)
  - over-pace:  used% > budget% -> unavailable (soft block)
  - exhausted:  remainingFraction <= 0 or used% >= 100% -> unavailable (hard block)
  - unknown:    malformed/missing data -> unavailable (hard block, fail-closed)

Dependency-free; no network.
"""

import os
import shutil
import sys
import tempfile
import types
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


q = tc.Quota.__new__(tc.Quota)

# Pin identity curve for clear arithmetic: budget == elapsed%
os.environ["EPSILONERIDANI_PACE"] = "0:0,100:100"

NOW = 1700000000.0


def iso(delta_s: float) -> str:
    return datetime.fromtimestamp(NOW + delta_s, tz=UTC).isoformat().replace("+00:00", "Z")


# 4 hours left out of 5 hours (18000s) -> 1 hour elapsed = 20%
RESET_4H = iso(4 * 3600)


def gemini_payload(rem: object, reset_time: object = RESET_4H, model: str = "gemini-3.1-pro-high") -> dict:
    return {
        "models": {
            model: {
                "quotaInfo": {
                    "remainingFraction": rem,
                    "resetTime": reset_time,
                }
            }
        }
    }


# -----------------------------------------------------------------------------
# 1. Extraction: _gemini_quota_info
# -----------------------------------------------------------------------------
p_std = gemini_payload(0.8)
qi = tc.quota._gemini_quota_info(p_std)
check("quota_info extracted for default model", qi is not None and qi.get("remainingFraction"), 0.8)

p_fallback = gemini_payload(0.75, model="gemini-custom-model")
qi_fb = tc.quota._gemini_quota_info(p_fallback)
check("quota_info fallback for other gemini model", qi_fb is not None and qi_fb.get("remainingFraction"), 0.75)

p_non_gemini = {"models": {"other-model": {"quotaInfo": {"remainingFraction": 0.5}}}}
check("quota_info ignored for non-gemini model", tc.quota._gemini_quota_info(p_non_gemini), None)

check("quota_info None on empty dict", tc.quota._gemini_quota_info({}), None)
check("quota_info None on non-dict", tc.quota._gemini_quota_info("not-a-dict"), None)

# -----------------------------------------------------------------------------
# 2. Cache expiry: _gemini_valid_until
# -----------------------------------------------------------------------------
# TTL is 600s; reset is in 4h (14400s) -> valid_until = NOW + 600
vu_far = tc.quota._gemini_valid_until(p_std, NOW)
check("valid_until bounded by TTL when reset is far", vu_far, NOW + 600)

# Reset is in 200s < 600s TTL -> valid_until = resets_at
p_soon = gemini_payload(0.8, reset_time=iso(200))
vu_soon = tc.quota._gemini_valid_until(p_soon, NOW)
check("valid_until bounded by resets_at when reset is sooner than TTL", round(vu_soon or 0.0), round(NOW + 200))

# Missing / invalid resetTime -> None
p_bad_reset = gemini_payload(0.8, reset_time="not-a-date")
check("valid_until None on bad resetTime", tc.quota._gemini_valid_until(p_bad_reset, NOW), None)

# -----------------------------------------------------------------------------
# 3. Pacing calculation & classification under identity curve (0:0,100:100)
# At 20% elapsed (1h of 5h): budget is 20%.
# -----------------------------------------------------------------------------
# Under-pace: remainingFraction = 0.9 -> used = 10% < 20% budget
prov_under = q._gemini_from_payload(gemini_payload(0.9), now=NOW)
check("under-pace: available", prov_under.available, True)
check("under-pace: model is default gemini", prov_under.model, "gemini-3.1-pro-high")
check("under-pace: status", [w.status for w in prov_under.windows], ["under-pace"])
check("under-pace: window name", prov_under.windows[0].name, "session")

# At-budget: remainingFraction = 0.8 -> used = 20% == 20% budget
prov_at = q._gemini_from_payload(gemini_payload(0.8), now=NOW)
check("at-budget: unavailable", prov_at.available, False)
check("at-budget: model is None", prov_at.model, None)
check("at-budget: status", prov_at.windows[0].status, "at-budget")
soft, why = tc._unavail_reason(prov_at)
check("at-budget: soft block", soft, True)
check(
    "at-budget: reason format",
    why,
    "session at budget (20% elapsed: used 20% = 20% pace budget), 80% left",
)

# Over-pace: remainingFraction = 0.7 -> used = 30% > 20% budget
prov_over = q._gemini_from_payload(gemini_payload(0.7), now=NOW)
check("over-pace: unavailable", prov_over.available, False)
check("over-pace: status", prov_over.windows[0].status, "over-pace")
soft, why = tc._unavail_reason(prov_over)
check("over-pace: soft block", soft, True)
check(
    "over-pace: reason format",
    why,
    "session ahead of pace (20% elapsed: used 30% > 20% pace budget), 70% left",
)

# Exhausted: remainingFraction = 0.0 -> used = 100%
prov_ex = q._gemini_from_payload(gemini_payload(0.0), now=NOW)
check("exhausted: unavailable", prov_ex.available, False)
check("exhausted: status", prov_ex.windows[0].status, "exhausted")
soft, why = tc._unavail_reason(prov_ex)
check("exhausted: hard block (not soft)", soft, False)
check("exhausted: reason format", why, "session exhausted")

# -----------------------------------------------------------------------------
# 4. Custom pace curve
# -----------------------------------------------------------------------------
os.environ["EPSILONERIDANI_PACE"] = "0:50,100:100"
# At 20% elapsed, budget is 50 + 0.2 * 50 = 60%.
# remainingFraction = 0.7 -> used = 30% < 60% budget -> under-pace!
prov_custom = q._gemini_from_payload(gemini_payload(0.7), now=NOW)
check("custom pace: used 30% < budget 60% is available", prov_custom.available, True)
check("custom pace: status under-pace", prov_custom.windows[0].status, "under-pace")
os.environ["EPSILONERIDANI_PACE"] = "0:0,100:100"

# -----------------------------------------------------------------------------
# 5. Fail-closed on malformed payloads
# -----------------------------------------------------------------------------
prov_no_qi = q._gemini_from_payload({}, now=NOW)
check("empty payload fails closed", prov_no_qi.available, False)
check("empty payload error note", prov_no_qi.error, "gemini usage response schema unsupported (no quotaInfo)")

prov_bad_rem = q._gemini_from_payload(gemini_payload("not-a-number"), now=NOW)
check("non-numeric remainingFraction fails closed", prov_bad_rem.available, False)
check("non-numeric remainingFraction status", prov_bad_rem.windows[0].status, "unknown")
soft, _ = tc._unavail_reason(prov_bad_rem)
check("unknown window is hard block", soft, False)

prov_bad_ts = q._gemini_from_payload(gemini_payload(0.9, reset_time="not-a-timestamp"), now=NOW)
check("unparseable resetTime fails closed", prov_bad_ts.available, False)
check("unparseable resetTime status", prov_bad_ts.windows[0].status, "unknown")

# -----------------------------------------------------------------------------
# 6. Next eligible calculation
# -----------------------------------------------------------------------------
# Exhausted window: next eligible is exactly the reset time
check("exhausted next_eligible == resets_at", round(prov_ex.next_eligible or 0.0), round(NOW + 14400))

# Over-pace window: next eligible is earlier than resets_at, when budget will equal used
check(
    "over-pace next_eligible is between now and resets_at",
    NOW < (prov_over.next_eligible or 0.0) < (NOW + 14400),
    True,
)

# -----------------------------------------------------------------------------
# 7. Quota.choose() priority and selection
# -----------------------------------------------------------------------------
tmp = Path(tempfile.mkdtemp())
cfg = types.SimpleNamespace(home=tmp, quota_cache=tmp)
q_mock = tc.Quota(cfg)

q_mock.codex = lambda refresh=False: tc.Provider("codex", False, None)
q_mock.claude = lambda refresh=False, renew=False: tc.Provider("claude", False, None)
q_mock.gemini = lambda refresh=False, renew=False: tc.Provider("gemini", True, "gemini-3.1-pro-high")

check("auto selects gemini when codex/claude unavailable", q_mock.choose("auto")[0], "gemini")
check("forced gemini selects gemini when available", q_mock.choose("gemini")[0], "gemini")

q_mock.gemini = lambda refresh=False, renew=False: tc.Provider("gemini", False, None)
check("forced gemini returns None when unavailable", q_mock.choose("gemini")[0], None)
check("auto returns None when all unavailable", q_mock.choose("auto")[0], None)

# Codex takes precedence over Gemini in auto:
q_mock.codex = lambda refresh=False: tc.Provider("codex", True, "gpt-5")
q_mock.gemini = lambda refresh=False, renew=False: tc.Provider("gemini", True, "gemini-3.1-pro-high")
check("auto prefers codex over gemini", q_mock.choose("auto")[0], "codex")

# -----------------------------------------------------------------------------
# 8. Quota.gemini() missing credentials / API key fallback
# -----------------------------------------------------------------------------
# Test fallback when GEMINI_API_KEY is present and no token exists
saved_key = os.environ.get("GEMINI_API_KEY")
try:
    os.environ["GEMINI_API_KEY"] = "mock-gemini-key"
    q_env = tc.Quota(cfg)
    q_env._gemini_token = lambda: None
    # Simulate agy on PATH
    saved_which = shutil.which
    shutil.which = lambda cmd: "/usr/bin/agy" if cmd == "agy" else saved_which(cmd)
    p_key = q_env.gemini()
    check("gemini with API key only is available", p_key.available, True)
    check("gemini with API key uses default model", p_key.model, "gemini-3.1-pro-high")
    check("gemini with API key has no pacing windows (unpaced)", p_key.windows, [])

    # Test missing credentials
    os.environ.pop("GEMINI_API_KEY", None)
    p_none = q_env.gemini()
    check("gemini with no credentials is unavailable", p_none.available, False)
    check("gemini with no credentials error", p_none.error, "no GEMINI_API_KEY or ~/.gemini/antigravity-cli")
finally:
    if saved_key is not None:
        os.environ["GEMINI_API_KEY"] = saved_key
    else:
        os.environ.pop("GEMINI_API_KEY", None)
    shutil.which = saved_which

print()
if fails:
    print(f"FAIL: {fails} mismatch(es)")
    sys.exit(1)
print("PASS: all tests passed")
