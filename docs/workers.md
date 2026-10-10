# Persistent workers

Persistent workers are declarative. You describe the workers you want in
`workers.toml`, and a manager process reconciles reality against that file:
starting missing workers, stopping removed ones, restarting changed ones, and
backing off repeated failures. They do not belong to a terminal session.

## Quickstart

Nothing needs to exist first. `workers add` creates the config, writes the
definition, and starts a manager:

```bash
epsiloneridani workers add                        # an enabled worker1, the whole cascade
epsiloneridani workers add reviewer --only review # a focused, named worker
epsiloneridani workers                            # desired and actual state
epsiloneridani workers logs --follow reviewer     # its durable console log
```

Day-to-day controls:

```bash
epsiloneridani workers disable reviewer   # persist desired stopped state
epsiloneridani workers enable reviewer    # and start it again
epsiloneridani workers restart reviewer   # without changing desired state
epsiloneridani workers remove reviewer    # drop the definition and stop it
```

## Editing the file directly

`workers add` cannot express every field, and it rewrites the file in canonical
form, dropping comments and hand formatting. To keep those, or to set a field
`add` does not cover, edit the file and apply it:

```bash
epsiloneridani workers edit          # $VISUAL, else $EDITOR, else vi
epsiloneridani workers apply --check # validate without reconciling
epsiloneridani workers apply         # reconcile, starting a manager if needed
```

`workers edit` does not start a manager on its own; `workers apply` is the
explicit follow-up. A later `enable`, `disable`, `add`, or `remove` normalizes
the file again, so comments do not survive one.

A small configuration might look like this. The repository also includes
[`workers.toml.example`](../workers.toml.example).

```toml
version = 1

[[workers]]
id = "worker1"
enabled = true

[[workers]]
id = "worker2"
enabled = true
agent = "codex"
only = ["rebase", "review"]
ignore_quota = true
```

You can edit `workers.toml` while the manager is running. It reloads the file
each cycle. If your edit does not validate, the manager keeps the last good
generation, leaves running workers alone, and says so once:

```
epsiloneridani workers: invalid configuration; keeping last good generation: ...
```

## Where the config lives

The first of these that is set wins:

| Source | Path |
| --- | --- |
| `epsiloneridani workers --config PATH` | exactly that file |
| `$EPSILONERIDANI_WORKERS_CONFIG` | exactly that file |
| `$EPSILONERIDANI_CONFIG_HOME` | `$EPSILONERIDANI_CONFIG_HOME/workers.toml` |
| `$XDG_CONFIG_HOME` | `$XDG_CONFIG_HOME/epsiloneridani/workers.toml` |
| macOS default | `~/Library/Application Support/epsiloneridani/workers.toml` |
| otherwise | `~/.config/epsiloneridani/workers.toml` |

`--config` belongs to `epsiloneridani workers` itself, not to the action, so it goes
before the action name: `epsiloneridani workers --config ./workers.toml apply`.

Writes take an `flock` on a sibling `workers.toml.lock`, then replace the file
atomically, so concurrent CLI and dashboard mutations serialize instead of
clobbering each other.

## `workers.toml` reference

The file supports two top-level keys. `version` is required and must be `1`;
`workers` is an optional array of tables and defaults to an empty array. Any
other top-level key is an error, as is any unrecognized field inside a
`[[workers]]` table. Duplicate ids are rejected.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `id` | string | required | `[a-z0-9-]+`, at most 40 characters; namespaces the worker's state, checkout, review store, and logs |
| `enabled` | bool | `true` | Desired running state. `false` stops the worker without forgetting it |
| `agent` | string | `"auto"` | `auto`, `codex`, `claude`, `kiro`, `deepseek`, or `minimax` |
| `only` | string list | `[]` | Work phases: `rebase`, `bump`, `progress`, `fix-ci`, `fix`, `review`, `roadmap`, `lint-repair`. Empty means the whole cascade |
| `sandbox` | string | `"host"` | `host` or `bubble`. Progress-report rounds always run on the host |
| `ignore_quota` | bool | `false` | Skip soft pacing. Provider hard limits still apply; an `auto` worker cannot launch with this enabled |
| `auto_refresh` | bool | `false` | Renew this worker's Claude access token when it expires, instead of parking until a human runs `claude`. Only safe when nothing else uses the same credential file — the refresh token is single-use, so pair it with `claude_config_dir`. See [quota and pacing](quota.md) |
| `roadmap_only` | string | unset | The single roadmap area for roadmap rounds. `""` means all areas; unset means a fresh random area each round |
| `roadmap_skip` | string list | `[]` | Roadmap areas to exclude. `roadmap_only` wins on overlap |
| `roadmap_extra_identities` | string list | `[]` | Extra GitHub logins whose claimed intentions count as this worker's own |
| `respect_claims` | bool | `true` | Whether to avoid intentions others have claimed |
| `source` | string | unset | Supplementary repository directory or URL. Requires `roadmap` in `only` and a non-empty `roadmap_only` |
| `author_model` | string | unset | Exact authoring model. Requires an `agent` other than `auto` |
| `author_effort` | string | unset | Authoring reasoning effort for Codex, Claude, or Kiro. Requires an explicit `agent` |
| `pace` | string | unset | Pacing curve as `time%:budget%` points, for example `0:10,50:70,90:90`; rejected by `apply --check` if malformed |
| `stream` | bool | `false` | Keep the agent transcript in the console log instead of a separate file |
| `isolate_home` | bool | `false` | Force credential isolation for the id `default`; every other id already enables it |
| `claude_config_dir` | string | unset | This worker's **own** Claude login: a Claude config directory holding `.credentials.json`, used instead of the operator's `~/.claude`. Absolute, or starting with `~`. Create it with `workers login ID`. Not on macOS. See [one Claude login per worker](#one-claude-login-per-worker) |
| `restart` | string | `"always"` | `always` after any exit, `on-failure` after a nonzero exit, or `never`; explicit restart and re-enable still work |
| `env` | table of strings | `{}` | Extra environment for this worker's process tree, for settings with no flag of their own. Values must be quoted strings, names POSIX-portable, and the table at most 16 KB. The variables the worker sets itself (`EPSILONERIDANI_MANAGED`, `EPSILONERIDANI_LOG_FILE`, `EPSILONERIDANI_PARENT_PIPE_FD`, `EPSILONERIDANI_DATA_HOME`, and the runtime-status path) are rejected. **Not a secret store** — see below |

The manager fingerprints each definition. It stops a worker when `enabled`
becomes false. When any other field changes, it restarts that worker **after its
current round**, without disturbing unchanged workers. See
[rolling out a change](#rolling-out-a-change-one-worker-at-a-time).

`env` exists for running one worker differently from its peers when the
difference has no flag: an A/B of a build setting, for instance. It is part of
the fingerprint, so editing it restarts that worker and leaves the others alone,
and both `workers status` and the dashboard name the variables it sets, so the
odd worker out is visible rather than mysterious.

Put no secrets in it. The values are stored in plain `workers.toml`, and the
whole worker definition is handed to its runner on a command line, where any
process running as you can read it. Credentials belong in the provider
mechanisms `epsiloneridani` already uses (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`,
`KIRO_API_KEY`), which keep them in files rather than argv. Status output prints
only the variable names for the same reason.

Every worker enables `LAKE_ARTIFACT_CACHE=1` and
`LAKE_RESTORE_ARTIFACTS=1` by default. The second setting belongs with the first:
with the artifact cache writable, Lake may keep a build product in its store
instead of the build directory, and EpsilonEridani's audits (`lake exe axioms`, `lake
exe module-system`) resolve `.olean`s through the Lean search path. Restoring the
copy keeps them working, and measured on a 1500-declaration module, returning to
a previously built state still fell from 4s of recompilation to 0s of restore.
An explicit value in a worker's `env` table overrides either default.

The trade is disk: old build generations remain in the store, and Lake supplies
no selective eviction — `lake cache clean` empties it all or nothing. EpsilonEridani
records the toolchain on canonical `main` for each worker and clears that
worker's default Lake cache when the pin changes. The first observed pin only
seeds the record; there is deliberately no size- or calendar-based cleanup. An
operator-supplied `LAKE_CACHE_DIR` is never deleted automatically.

## `workers add` flags

`add` takes an optional id, defaulting to the next free `workerN`, and writes an
entry with `enabled = true`.

| Flag | Sets |
| --- | --- |
| `worker_id` | `id`; omit it for the next free `workerN` |
| `--agent AGENT` | `agent` |
| `--only TASKS` | `only`, as a comma-separated list |
| `--sandbox {host,bubble}` | `sandbox` |
| `--ignore-quota` | `ignore_quota`; use with an explicit subscription agent |
| `--auto-refresh` | `auto_refresh` |
| `--roadmap-only AREA` | `roadmap_only` |
| `--roadmap-skip AREAS` | `roadmap_skip`, as a comma-separated list |
| `--source PATH_OR_URL` | `source`; also requires `roadmap` in `--only` and a non-empty `--roadmap-only` |
| `--author-model MODEL` | `author_model` |
| `--author-effort EFFORT` | `author_effort`; Codex, Claude, or Kiro only |
| `--pace CURVE` | `pace` |
| `--stream` | `stream` |
| `--isolate-home` | `isolate_home`; useful when the id is `default` |
| `--claude-config-dir DIR` | `claude_config_dir` |

`add` cannot set `roadmap_extra_identities`, `respect_claims`, or `restart`, and
always writes `enabled = true`. Use `workers edit` for those.

## Actions

| Action | What it does |
| --- | --- |
| _(none)_ | Same as `status` |
| `status [--json] [--watch]` | Desired and actual state, plus a hint when a newer release is on PyPI or a worker still runs older code (see [upgrading](#upgrading)). Exits nonzero if the manager is offline or a wanted worker is not alive |
| `apply [--check]` | Validate the TOML schema and manager-level rules, then reconcile. `--check` validates only |
| `add [ID] [flags]` | Append an enabled definition and reconcile |
| `enable ID` / `disable ID` | Persist desired running or stopped state |
| `restart ID \| --all [--after-round]` | Request a restart without changing desired state; a disabled worker stays stopped. `--all` is every enabled worker. Immediate by default, which stops the round in flight. `--after-round` lets the worker finish its round first |
| `drain ID... \| --all [--wait] [--timeout S]` | Stop workers between rounds: each finishes the round it is in, then stays down until `resume`. `--wait` returns once all have stopped, or exits 1 after `--timeout` (default 7200s). See [draining for a restart](#draining-for-a-restart) |
| `resume ID... \| --all` | Clear a drain. A worker still finishing its round carries on; a stopped one is launched again |
| `remove ID` | Drop the definition and stop the worker |
| `login ID` | Open `claude` on the worker's `claude_config_dir` so you can `/login` it into its own account, then confirm the login is renewable |
| `logs ID [--follow] [--lines N]` | The durable console log. `--follow` continues across worker restarts |
| `tmux [--no-attach]` | Build the optional tmux viewing workspace |
| `manager [--interval N]` | Run the reconciler in the foreground |
| `manager-stop [--leave-workers]` | Stop the detached manager. By default it also stops its workers |
| `service ACTION` | `install`, `uninstall`, `start`, `stop`, `restart`, or `status` for the native user service |
| `edit [--editor CMD]` | Create or open `workers.toml` in an editor; it does not apply the result |
| `import LEGACY [--force]` | One-shot migration from the legacy `workers.conf` format; it does not start a manager |

`apply` without `--check`, `add`, `enable`, `disable`, `remove`, and `restart`
start a manager if none is running and return only after it accepts control
requests. `edit`, `import`, and `apply --check` do not start one.

## The manager, and running past logout

`workers apply` and related actions start a detached manager owned by the current
login session. To move an existing fleet to a native user service that survives
logout and comes back after a reboot, leave its workers running while the service
takes over the manager socket:

```bash
epsiloneridani workers manager-stop --leave-workers
epsiloneridani workers service install
epsiloneridani workers service status
```

Omit `manager-stop` when no detached manager is running. One runtime directory
can host only one manager and one active configuration at a time. A reconcile
action that names a different `--config` file reports the conflict instead of
switching it.

That is a systemd user service on Linux and NixOS, or a LaunchAgent on macOS.
Two platform caveats:

- On Linux systems that stop the user manager after the last logout,
  `loginctl enable-linger "$USER"` keeps user services running with no login
  session.
- A macOS LaunchAgent survives terminal and SSH-session loss, but starts only
  after a graphical login. Installation says so explicitly when no GUI login
  domain is active.

At startup the worker preserves an explicit `SSL_CERT_FILE` or discovers the
host's system CA bundle, including NixOS's `/etc/ssl/certs/ca-certificates.crt`,
so quota checks keep working when uv's standalone Python defaults to a different
OpenSSL trust store. A shell-provided Nix bundle carried into the service stays
a revalidated candidate rather than a pinned trust path, so garbage collection
cannot silently disable fallback discovery.

The reconciler and the worker control sockets are portable Unix code. No Linux
`/proc` interface is required.

## Draining for a restart

Stopping a worker stops its round with it. `disable`, `restart`,
`manager-stop` and stopping the service all send SIGTERM, which tears down the
round in flight and throws away whatever its agent had not pushed yet.

`drain` stops workers *between* rounds instead. Each one finishes the round it
is in, then exits instead of starting another. A worker that is waiting (on
quota, a GitHub reset, or the pause after a round) stops within a couple of
seconds. The manager leaves a drained worker down until you `resume` it:

```bash
epsiloneridani workers drain --all --wait      # every round in flight runs to completion
systemctl --user stop epsiloneridani-workers   # nothing is running any more
# ... upgrade, edit the unit, reboot ...
systemctl --user start epsiloneridani-workers
epsiloneridani workers resume --all
```

A drain is a file, `<id>.drain` beside the worker's status file in the state
directory, which its loop checks before every round. So it needs no manager, and
it outlasts a manager or service restart: workers drained before a restart stay
drained after it until resumed. `workers status` shows them as `draining` while
they finish and `drained` once stopped, and counts a drained worker as healthy,
as it does a disabled one. `resume` clears the file. A worker that already
stopped is launched again, even one with `restart = "on-failure"` or `"never"`,
whose clean exit would otherwise be final.

## Rolling out a change one worker at a time

A changed definition reaches a worker gracefully. Edit one worker's entry in
`workers.toml` (its `env`, model, phases or login), and the manager asks that
worker to finish the round it is in. Once it has, the manager starts the new
definition. The other workers keep running. To roll a change across the fleet,
edit one worker, watch it come back in `workers status` (`restarting after its
current round` until then), and carry on with the next.

`workers restart ID --after-round` does the same without a definition change,
for example to pick up a new install after an upgrade. Plain `workers restart ID`
still restarts at once. Disabling or removing a worker still stops it at once.
A worker that is drained when its definition changes stays down until `resume`,
as the drain asked.

A graceful restart uses the same between-rounds stop as `drain`. A worker that
cannot honour it (one older than drain support, 0.9.0, or a hung loop) is stopped
outright once the restart has waited `ROUND_TIMEOUT` plus ten minutes, and then
restarted. So a rollout never stalls, and no healthy round is cut short, since
every round ends within `ROUND_TIMEOUT`. `$EPSILONERIDANI_GRACEFUL_RESTART_TIMEOUT`
sets that wait in seconds.

Settings in the service's own environment (a systemd drop-in, say) are not part
of any worker's definition. A change there reaches workers only when the manager
restarts, which is what `workers drain --all --wait` is for. To roll such a
setting out gradually instead, put it in each worker's `env` table.

## Upgrading

`workers status` and the dashboard check PyPI for a newer `epsiloneridani` at
most once a day (a failed lookup is retried after six hours) and, when one is
out, say so:

```text
update:  epsiloneridani 0.17.0 is available (installed 0.16.0): upgrade with
         `/path/to/python -m pip install -U epsiloneridani`, then
         `epsiloneridani workers restart --all --after-round`
```

The command matches how this copy was installed: pip (including a VCS
install, which it moves onto the release), `uv tool upgrade`, or
`pipx upgrade`. An editable checkout is told to update the checkout instead.
Only final releases are suggested. Nothing is ever installed for you: an
upgrade changes the code that decides when the workers spend, so it stays your
call. `EPSILONERIDANI_NO_UPDATE_CHECK=1` turns the check off; a source tree
that is not installed (the Docker image) is never checked.

Upgrading the package does not touch running workers: each keeps the code it
started with. Every worker records the version it launched on, and until it
restarts, `workers status` marks it `(running 0.16.0; restart to pick up
0.17.0)` and the dashboard lists it. `workers restart --all --after-round`
lets each finish its round and then starts it on the new code. The manager
process keeps running the old code too; when a release changes the manager
itself, restart it the way [draining for a restart](#draining-for-a-restart)
shows, so no round is cut short.

## Worker ids and credential isolation

Every worker id namespaces its state, checkout, review store, logs, and Bubble
home. Workers coordinate through GitHub rather than sharing local mutable state,
so several workers can use one host without sharing a checkout. A maintenance
branch claim lives in the PR head repository, where the worker that can update
the branch can also maintain its lease; roadmap and progress claims use the
canonical repository by default. Review markers, claims, and compare-and-swap
push and PR helpers keep workers from overwriting one another when they select
the same target.

Every id other than `default` also enables credential isolation. On Linux and
other non-macOS hosts, the worker gets a private `$HOME` containing private
Claude and Codex credential copies. GitHub CLI and Git configuration remain
shared so the worker still acts as the operator's `gh` account. `--isolate-home`
applies the same isolation when the id is literally `default`.

The Lean build caches are handled outside that isolation, in two different
ways, because they are written differently.

Toolchains are shared outright: `$ELAN_HOME` points at the login user's
`~/.elan`, so one install serves every worker and your own checkouts. Installing
a toolchain takes a per-toolchain lock and lands by rename, so workers racing for
the same one is safe. Left per-worker, 22 installs covered 6 distinct toolchains.

Mathlib's `.ltar` cache is *not* shared as a download target. `lake exe cache
get` takes no lock, and in any checkout older than
https://github.com/leanprover-community/mathlib4/pull/42752 it writes fixed-name
temporary files, so two workers downloading into one directory can leave a
corrupt `.ltar` under a name every later run trusts — including yours. Each
worker therefore downloads into its own `$MATHLIB_CACHE_DIR` under its state
directory, and before each round it exchanges *finished* files with the pool by
hardlink: it promotes what it fetched last round and takes a link to everything
the pool has that it lacks. A hardlink is atomic and an existing name is never
replaced, so the pool only ever gains complete artifacts, costs no extra disk,
and a worker downloads only what nobody on the machine has. Left per-worker
entirely, half of one week's 10.2 GB of `.ltar` traffic was a file another worker
already had.

`$LAKE_CACHE_DIR` stays per-worker too. It normally sits under the toolchain
directory, and would otherwise follow `$ELAN_HOME` into the pool, but unlike an
install it is written throughout every build. That keeps the default
`LAKE_ARTIFACT_CACHE` store private to the worker that fills it. Once canonical
`main` moves to a different `lean-toolchain`, the next host checkout preparation
clears this private store while no agent is
running. Switching between PR branches does not count as a bump and therefore
does not churn the cache.

Setting any of the three yourself overrides this. `scripts/share-build-caches`
folds the private copies an already-running fleet accumulated into the pool
without re-downloading them.

On macOS, `$HOME` stays unchanged because both Claude Code and GitHub CLI use the
login Keychain. `epsiloneridani` redirects `$CLAUDE_CONFIG_DIR` and `$CODEX_HOME`, which
isolates Codex, but host workers still share the login user's Claude account.
Bubble rounds copy that shared Claude credential into a private transient
directory without modifying the Keychain. See [the sandbox notes](sandbox.md)
for that handoff.

What an isolated worker inherits is the credential, not your configuration. Its
`.claude` directory gets a `settings.json` of its own, no `CLAUDE.md`, and only
the one `pi` skill the worker itself dispatches through, so a round behaves the
same whoever ran it and your personal instructions cannot contradict the task
prompt. Edit that generated `settings.json` in place to tune a worker; it is
written once and never overwritten. `EPSILONERIDANI_INHERIT_CLAUDE_CONFIG=1` restores
the previous behaviour of sharing your own `CLAUDE.md`, settings, and skills,
including on a worker already seeded the clean way; a `settings.json` you have
edited is kept rather than replaced.

## One Claude login per worker

By default every Claude worker draws its tokens from the operator's `~/.claude`,
the same OAuth grant your interactive `claude` uses. An isolated worker holds
only a copy of the *access* token, with the refresh token stripped. Renewing a
grant retires its previous access token. So when anything renews that shared
login, every Claude round then in flight fails with
`401 OAuth access token has been revoked`. "Anything" includes your own
`claude`, or another worker with `auto_refresh`. One worker with `auto_refresh`
is enough to hit the others, and every extra Claude worker makes a collision
more likely.

Giving each Claude worker its own login removes the sharing:

```toml
[[workers]]
id = "worker1"
agent = "claude"
auto_refresh = true
claude_config_dir = "~/.config/epsiloneridani/claude/worker1"
```

```console
$ epsiloneridani workers login worker1    # opens claude; run /login, then /exit
$ epsiloneridani workers restart worker1
```

Each login is a separate OAuth grant on the same account, so the worker still
draws on your quota. With `auto_refresh`, the pacer renews only that grant, and
only between that worker's own rounds, which never overlap each other. It renews
`ROUND_TIMEOUT` plus 30 minutes ahead of expiry; set `$CLAUDE_REFRESH_SKEW_S` to
change that. It also does not launch a round on a token that cannot outlast
`ROUND_TIMEOUT`, which is what a failed renewal leaves behind. So no round
starts only to end in a 401.

A worker that already ran on the shared login keeps its credential copy pinned
to that source. Naming `claude_config_dir` is the one change it follows: the
next start re-seeds the copy from the new login. Any other change of
`$CLAUDE_CONFIG_DIR` still only warns.

`workers status` shows each worker's login and when its refresh token expires.
That happens a fixed time after the browser sign-in, so expect to repeat
`workers login` about monthly. Status flags a login within three days of
expiry. It also warns when two enabled Claude workers share a login, or when a
worker with `auto_refresh` renews the operator's own `~/.claude`.

Not on macOS, where Claude Code keeps its login in the Keychain rather than in
the config directory.

## State on disk

| Path | Contents |
| --- | --- |
| `<state>/<id>.json` | Per-worker status, including its host, heartbeated every two seconds |
| `<state>/manager.heartbeat` | The manager's host and a heartbeat, every two seconds |
| `<state>/manager.log` | The detached manager's own console |
| `<state>/logs/<id>/work-*.log` | Durable per-run console logs |
| `<runtime>/manager.sock`, `w-<id>.sock` | Control sockets, mode 0600 |

`<state>` is `$EPSILONERIDANI_WORKERS_STATE_DIR`, else `$XDG_STATE_HOME/epsiloneridani/workers`,
else `~/Library/Application Support/epsiloneridani/state/workers` on macOS, else
`~/.local/state/epsiloneridani/workers`. `<runtime>` is `$EPSILONERIDANI_RUNTIME_DIR`, else
`$XDG_RUNTIME_DIR/epsiloneridani`, else `/tmp/epsiloneridani-$(id -u)`; it must be owned by you
and inaccessible to other users, or the manager refuses to start.

Each managed worker publishes a structured state such as `waiting-quota`,
`surveying`, `running`, or `backoff`, along with its current phase and target and
the path to its logfile. That is what `epsiloneridani workers status` and the
dashboard's workers view read.

### Several nodes, one shared home

On a cluster, `<state>` is usually on a home filesystem that every node mounts, while
`<runtime>` is per node. The status files therefore reach every node, but the
control sockets do not. Seen from another node, `epsiloneridani workers status`
shows
`manager: running on <host> (heartbeat 3s ago; live details and control only there)`,
and each worker as `(on <host>)` with the state it last reported. A heartbeat older
than 30 seconds does not count, because its node may have gone down.

The same heartbeat prevents a second manager. `epsiloneridani workers manager` refuses
to start while another node's manager is heartbeating. A command that would start one,
such as `workers add` or `resume`, instead notes where the manager runs. That
manager re-reads the shared `workers.toml` on every pass, so nothing needs
applying from here. Drain and resume markers are files in `<state>`, so they work
from any node. Starting, stopping or restarting the service itself still has to
happen on its own node.

## tmux is a viewer, not the supervisor

`epsiloneridani workers tmux` opens one dashboard window plus one log-following window
per enabled worker. Killing that session does not stop any worker, and running
the command again rebuilds the view from the configuration and the durable logs.
tmux is optional; workers run without it.

## Migrating from `workers.conf`

`workers.conf` is a legacy line-oriented format: one `epsiloneridani work --loop`
command per line, each with an explicit `--worker-id`. It is only ever read by
the one-shot import, which refuses to overwrite an existing `workers.toml`
without `--force`:

```bash
epsiloneridani workers import workers.conf
```

Anything the legacy parser does not recognize is an error naming the file, line,
and token, so a partial migration is never silently accepted.
