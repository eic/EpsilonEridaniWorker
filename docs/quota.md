# Quota and pacing

The README covers the pacing rule and the controls. This page covers where the
usage numbers come from and how the pacer behaves at the edges.

## Credential sources

The pacer reads the credential files the official CLIs already maintain,
`~/.claude/.credentials.json` and `~/.codex/auth.json`, and queries each
provider's usage endpoint. It honors `$CLAUDE_CONFIG_DIR`, so switching between
a personal and a work Claude account is paced correctly.

Reading quota never spends anything, with the one exception described under
"window bootstrap" below. `epsiloneridani status`, the dashboard, and an auto selection
that lands on Codex make no model request at all.

## Keeping the Claude token alive: `--auto-refresh`

By default the worker never rotates a refresh token. When the Claude access token
expires it reports Claude unavailable, and your next `claude` run renews it. That
is fine at a keyboard and fatal unattended: `work --loop` will sit at
`claude usage HTTP 401 (access token expired or rejected; log in again)` until
someone intervenes.

`epsiloneridani work --loop --auto-refresh` (or `$EPSILONERIDANI_AUTO_REFRESH=1`) lets the
worker renew the token itself once it is within one round timeout plus 30
minutes of expiry: 2 hours by default, and `$CLAUDE_REFRESH_SKEW_S` overrides
it. The margin has to cover a whole round, because the round's agent holds no
refresh token and cannot renew mid-round. For the same reason the worker does
not launch a Claude round on a token that cannot outlast `ROUND_TIMEOUT`. It
waits for the next renewal attempt instead of starting a round that would end in
a 401.

**Only turn it on when nothing else uses that credential file.** Claude and Codex
issue single-use refresh tokens: exchanging one retires it and returns a
replacement. EpsilonEridani serializes its own processes on the host, but it cannot
serialize an interactive `claude` sharing `~/.claude/.credentials.json`, a second
refresher, or a copy of the credential on another machine — a rotation here logs
any of those out. The shape this is meant for is a worker running as its own
user, with its own Claude account nobody signs into interactively;
`$CLAUDE_CONFIG_DIR` gives the same separation on a shared login. With several
Claude workers, give each its own login (`claude_config_dir`, see
[one Claude login per worker](workers.md#one-claude-login-per-worker)).
Otherwise a renewal by one revokes the token every other worker's in-flight
round holds. On macOS the
flag does nothing: the Keychain is the store, and the section below applies
instead.

When it is on: it renews the file the operator owns, never a worker's stripped
mirror, and it never touches a credential carrying no refresh token, so the
Docker deployment's dedicated refresher stays the single writer there. Rotations
are rate-limited by markers beside the credential, shared across every worker on
the host. Only the paths about to run something renew — the loop pacing towards a
round, a round resolving the model it will launch, and the launch stage. Reading
commands stay reads: `epsiloneridani status` and the dashboard report an expired token
rather than rotating it behind you.

## macOS and the login Keychain

On macOS, Claude Code keeps its credentials in the login Keychain rather than in
a file. The pacer reads them from the Keychain, read-only. It never refreshes
the Keychain, because that would log out your interactive `claude`. So on token
expiry it simply reports Claude unavailable for the cycle, and your next `claude`
run refreshes the Keychain so the pacer can read it again. `--auto-refresh` does
nothing here, so that run is the only way back.

A locked Keychain, which is what you get headless or over SSH, reports
unavailable with a hint to `security unlock-keychain` first.

Bubble uses the credential file directly where Claude stores credentials in a
file; on macOS it receives the Keychain credential through the private handoff
described in [the sandbox notes](sandbox.md).

## Reading Claude's two windows

Claude's session and weekly windows reset on separate clocks, so they are read
independently and neither is inferred from the other. Each window's raw state is
kept before any pacing is applied.

The structured `limits` array is authoritative for each window. Legacy flat keys
are used only when `limits` omits that window; they cannot override a structured
entry.

A missing window or invalid data, such as an unreadable reset timestamp or
non-numeric usage, stops the provider and reports the specific problem:

```
weekly limit missing from usage response
session reset timestamp invalid
```

rather than a generic "usage unknown". An unreadable constraint is not the same
as no constraint.

## The window bootstrap

There is one gap where the endpoint reports a window with no usage and no reset
clock: right after that window rolls. Only a Claude request can open the new
window, so `epsiloneridani` makes one small `claude -p` turn to do it, drops the cached
usage, and re-reads. The fresh telemetry, not the request, then decides whether a
round runs.

The bootstrap runs only under these conditions:

- It happens at the launch stage of a round that has already found work, so a
  poll that finds nothing to run costs nothing.
- Every other window must be active with real headroom. A window that is at
  budget, over pace, exhausted, missing, or unreadable forbids the bootstrap.
- It respects your pace curve. Under a curve whose budget stays at 0 for the
  first stretch of a window, say `--pace 0:0,90:0,100:95`, a fresh window may not
  be opened at all, and the status says so (`pace budget stays 0% through 90% of
  the window`) rather than quietly opening one to manufacture a clock.
- It is claimed under a lock in a shared ledger beside your credentials *before*
  the request goes out, so every worker on that account, whatever its worker id,
  checkout, or isolated `$HOME`, makes at most one request per window period,
  even if one of them crashes mid-flight.

If the window still is not reporting afterwards, the status reads
`session bootstrap attempted; awaiting fresh usage` and the worker stays parked.

## Sharing one usage reading

Workers that read the same Claude credential source also share their usage
readings. These are files in `.epsiloneridani-quota/` beside that source, next to
the bootstrap reservation. Claude's usage endpoint rate-limits hard: a `429` can
ask for up to an hour's wait. Each worker polling it separately multiplied the
fleet's requests for the same answer, and then every worker found out about the
429 separately.

- **One request at a time.** A lock serializes the reads, so when several workers
  poll together one asks and the rest reuse its answer.
- **A sibling's fresh reading is reused** instead of fetched while it is at most
  `$EPSILONERIDANI_CLAUDE_USAGE_SHARE_S` seconds old (default 300). It is paced as
  of the moment it was fetched, never the present, so reusing it cannot make a
  launch look more permitted than it was. It is checked exactly like a worker's
  own cache.
- **A sibling's 429 holds every worker back** until its `Retry-After` has passed.
  Meanwhile each worker answers from its own valid cache, as it would after a 429
  of its own.

A different access token (an account switch) is never served another token's
reading. A bootstrap request clears the shared reading along with the worker's
own. If the shared directory cannot be used, each worker reads on its own, as
before. Workers on their own logins (`claude_config_dir`) each have their own
source, so they share nothing with each other.

## Accounts with no weekly cap: `--claude-no-weekly-cap`

Some Claude seats only ever meter the 5-hour session window. Their usage
endpoint's weekly fields (`seven_day` and friends, and any `weekly` entry in
the structured `limits` array) stay `null` permanently, not just through the post-reset gap
the bootstrap above is for. A single reading cannot tell "idle, about to open"
apart from "this account has no weekly window at all", so `epsiloneridani`
does not try to infer it: left alone, the weekly window reads `idle` forever,
the bootstrap retries once an hour without ever resolving it, and `claude`
never goes available even though the session window it actually reports is
healthy.

If your account's usage page shows only a 5-hour limit and no weekly one, tell
the worker with `epsiloneridani work --loop --claude-no-weekly-cap` (or
`$EPSILONERIDANI_CLAUDE_NO_WEEKLY_CAP=1`, inherited by loop children). This
drops the weekly window from every reading entirely — it is not read, not
gated on, and never bootstrapped — and availability is decided from the
session window alone.

Check before you set it: most plans meter both windows. The
[Team Plan for Scientists](https://claude.com/programs/team-plan-for-scientists),
for example, has a 5-hour window and a weekly one, and its `seven_day` field
is a live reading. If `epsiloneridani workers status` (or the usage endpoint)
shows a non-null weekly percentage, leave the flag off. With the flag set on
such an account, the worker paces only against the 5-hour window and can use
up the week's allowance days before it resets.

## Why "strictly under"

A provider is available while `used%` is strictly under the budget for the
elapsed fraction of the window. Strictly, because the request being decided
costs something: sitting exactly on the budget
(`session at budget (20% elapsed: used 50% = 50% pace budget)`) is a pause, not a green light.

If usage cannot be read at all, the provider is treated as unavailable rather
than assumed free.
