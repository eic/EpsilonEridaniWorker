#!/usr/bin/env python3
"""A Claude login per worker: workers that share one OAuth grant revoke each other's access tokens on
every renewal, so a worker can name its own login (claude_config_dir / --claude-config-dir). This covers
the spec field, selecting the login before isolation, re-seeding a worker that moves to it, renewal
targeting that login rather than the mirror, the launch guard against a token that cannot outlive a
round, the renewal skew, and the status warnings about shared logins."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import epsiloneridani_worker as tc  # noqa: E402
from epsiloneridani_worker import worker_manager as wm  # noqa: E402

fails = 0


def check(name, got, expect):
    global fails
    ok = got == expect
    print(f"[{'OK ' if ok else 'XX '}] {name}: {got}")
    if not ok:
        print(f"      expected: {expect}")
        fails += 1


def login(directory: Path, access: str, refresh: str | None = "r", refresh_expires_ms: int | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    block = {"accessToken": access, "expiresAt": int((time.time() + 8 * 3600) * 1000)}
    if refresh:
        block["refreshToken"] = refresh
    if refresh_expires_ms is not None:
        block["refreshTokenExpiresAt"] = refresh_expires_ms
    (directory / ".credentials.json").write_text(json.dumps({"claudeAiOauth": block}))
    return directory


def dies(fn):
    try:
        fn()
    except tc.Die as error:
        return str(error)
    return None


# An operator's shell may export these; each case below sets exactly what it needs.
for name in ("EPSILONERIDANI_AUTO_REFRESH", "EPSILONERIDANI_DATA_HOME", tc.agents.WORKER_CLAUDE_LOGIN_ENV):
    os.environ.pop(name, None)

# --- the workers.toml field --------------------------------------------------------------------------
spec = wm.WorkerSpec.from_dict({"id": "w1", "agent": "claude", "claude_config_dir": "~/logins/w1"}, 0)
check("claude_config_dir expands ~", spec.claude_config_dir, os.path.expanduser("~/logins/w1"))
check("claude_config_dir survives as_dict", wm.WorkerSpec.from_dict(spec.as_dict(), 0), spec)
argv = spec.work_argv()
at = argv.index("--claude-config-dir") if "--claude-config-dir" in argv else -1
check("work_argv passes it to the worker", argv[at + 1] if at >= 0 else None, spec.claude_config_dir)
check("a worker without it passes nothing", "--claude-config-dir" in wm.WorkerSpec(id="w2").work_argv(), False)
try:
    wm.WorkerSpec.from_dict({"id": "w1", "claude_config_dir": "relative/dir"}, 0)
    relative = None
except wm.WorkersError as error:
    relative = str(error)
check("a relative claude_config_dir is refused", "absolute path" in (relative or ""), True)

tmp = Path(tempfile.mkdtemp())
wids = [f"claude-login-test-{os.getpid()}-{n}" for n in range(3)]
for wid in wids:
    shutil.rmtree(tc.HERE / "state" / wid, ignore_errors=True)
try:
    real = tmp / "realhome"
    (real / ".codex").mkdir(parents=True)
    shared = login(tmp / "shared-claude", "SHARED", "r-shared")
    own = login(tmp / "own-claude", "OWN", "r-own")

    def fresh_driver(config_dir: Path) -> None:
        """What a newly started loop driver sees: the operator's environment, not yet isolated."""
        os.environ["HOME"] = str(real)
        os.environ["CLAUDE_CONFIG_DIR"] = str(config_dir)
        for name in (
            "EPSILONERIDANI_DATA_HOME",
            tc.agents.WORKER_CLAUDE_LOGIN_ENV,
            # The other redirects a previous isolation exported, which a fresh driver would not have.
            "CODEX_HOME",
            "EPSILONERIDANI_KIRO_HOME",
            "EPSILONERIDANI_KIRO_DATA_DIR",
            "EPSILONERIDANI_KIRO_XDG_DATA_HOME",
            "EPSILONERIDANI_KIRO_PROCESS_HOME",
        ):
            os.environ.pop(name, None)

    # --- selecting the login before isolation ---------------------------------------------------------
    fresh_driver(shared)
    missing = dies(lambda: tc.use_worker_claude_login("w1", tmp / "nobody"))
    check("a missing login stops the worker", "no Claude login" in (missing or ""), True)
    check("...and says how to log in", "workers login w1" in (missing or ""), True)
    login(tmp / "access-only", "A", refresh=None)
    stripped = dies(lambda: tc.use_worker_claude_login("w1", tmp / "access-only"))
    check("a login without a refresh token stops the worker", "no refresh token" in (stripped or ""), True)
    check("a refused login leaves $CLAUDE_CONFIG_DIR alone", os.environ["CLAUDE_CONFIG_DIR"], str(shared))
    os.environ["EPSILONERIDANI_DATA_HOME"] = str(tmp / "already")
    tc.use_worker_claude_login("w1", tmp / "nobody")  # must not raise: a round child inherits isolation
    check("a round child is left to its parent's isolation", os.environ["CLAUDE_CONFIG_DIR"], str(shared))

    # --- a new worker on its own login ----------------------------------------------------------------
    fresh_driver(shared)
    tc.use_worker_claude_login(wids[0], own)
    iso = tc.isolate_home(wids[0]) / ".claude"
    check(
        "isolation seeds the copy from the worker's own login",
        tc.quota._read_json_file(iso / ".credentials.json")["claudeAiOauth"]["accessToken"],
        "OWN",
    )
    check(
        "isolation records the worker's own login as its source",
        (iso / ".epsiloneridani-creds-source").read_text(),
        str(own),
    )

    # --- an existing worker moving from the shared login to its own -----------------------------------
    fresh_driver(shared)
    iso = tc.isolate_home(wids[1]) / ".claude"
    check("a worker starts on the shared login", (iso / ".epsiloneridani-creds-source").read_text(), str(shared))
    fresh_driver(shared)
    tc.use_worker_claude_login(wids[1], own)
    tc.isolate_home(wids[1])
    check("naming its own login re-points the source", (iso / ".epsiloneridani-creds-source").read_text(), str(own))
    check(
        "...and re-seeds its copy",
        tc.quota._read_json_file(iso / ".credentials.json")["claudeAiOauth"]["accessToken"],
        "OWN",
    )

    # An incidental $CLAUDE_CONFIG_DIR change is not a deliberate move: the old pin and warning stay.
    fresh_driver(shared)
    iso = tc.isolate_home(wids[2]) / ".claude"
    fresh_driver(own)
    tc.isolate_home(wids[2])
    check(
        "an incidental change keeps the recorded source",
        (iso / ".epsiloneridani-creds-source").read_text(),
        str(shared),
    )

    # --- renewal targets the worker's own login, and the launch guard -----------------------------------
    fresh_driver(shared)
    tc.use_worker_claude_login(wids[0], own)
    home = tc.isolate_home(wids[0])  # an already-seeded worker: no copy, no re-seed
    quota = tc.quota.Quota(types.SimpleNamespace(home=home, quota_cache=tmp / "quota"))
    check("the pacer renews the worker's own login, never the mirror", quota._claude_creds_source(), own)

    soon = {"expiresAt": int((time.time() + 600) * 1000)}
    later = {"expiresAt": int((time.time() + tc.quota.ROUND_TIMEOUT + 600) * 1000)}
    check("without --auto-refresh nothing is held back", quota._claude_token_too_short(soon), None)
    os.environ["EPSILONERIDANI_AUTO_REFRESH"] = "1"
    held = quota._claude_token_too_short(soon)
    check("a token that cannot outlive a round holds the launch back", "shorter than a round" in (held or ""), True)
    check("a token that outlives a round launches", quota._claude_token_too_short(later), None)
    check(
        "a seconds-valued expiry is read as seconds",
        quota._claude_token_too_short({"expiresAt": time.time() + 600}) is not None,
        True,
    )
    login(own, "OWN", refresh=None)  # someone else renews this source: the pacer cannot, so it does not hold back
    check("a source the pacer cannot renew is not held back", quota._claude_token_too_short(soon), None)
    os.environ.pop("EPSILONERIDANI_AUTO_REFRESH")
finally:
    for wid in wids:
        shutil.rmtree(tc.HERE / "state" / wid, ignore_errors=True)

# --- the renewal skew -------------------------------------------------------------------------------
probe = "from epsiloneridani_worker import quota as q; print(q.CLAUDE_REFRESH_SKEW_S, q.ROUND_TIMEOUT)"
clean = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_REFRESH_SKEW_S", "EPSILONERIDANI_ROUND_TIMEOUT")}
out = subprocess.run([sys.executable, "-c", probe], cwd=REPO, env=clean, capture_output=True, text=True).stdout.split()
check("the default skew outlasts a whole round", [int(x) for x in out], [5400 + 1800, 5400])
clean["CLAUDE_REFRESH_SKEW_S"] = "9000"
out = subprocess.run([sys.executable, "-c", probe], cwd=REPO, env=clean, capture_output=True, text=True).stdout.split()
check("$CLAUDE_REFRESH_SKEW_S overrides it", out[0] if out else None, "9000")

# --- status: where each worker's tokens come from, and who else renews them ----------------------------
try:
    shared = login(tmp / "shared-claude", "SHARED", "r-shared")
    os.environ["CLAUDE_CONFIG_DIR"] = str(shared)
    far = int((time.time() + 20 * 86400) * 1000)
    near = int((time.time() + 3600) * 1000)
    snapshots = [
        {"id": "w1", "desired": "running", "spec": {"agent": "claude", "auto_refresh": True}},
        {"id": "w3", "desired": "running", "spec": {"agent": "auto"}},
        {"id": "w5", "desired": "stopped", "spec": {"agent": "claude"}},
        {"id": "g", "desired": "running", "spec": {"agent": "gemini"}},
        {
            "id": "own1",
            "desired": "running",
            "spec": {
                "agent": "claude",
                "auto_refresh": True,
                "claude_config_dir": str(login(tmp / "o1", "X", refresh_expires_ms=far)),
            },
        },
        {"id": "own2", "desired": "running", "spec": {"agent": "claude", "claude_config_dir": str(tmp / "o2")}},
        {
            "id": "own3",
            "desired": "running",
            "spec": {"agent": "claude", "claude_config_dir": str(login(tmp / "o3", "Y", refresh_expires_ms=near))},
        },
    ]
    peers = wm._claude_login_peers(snapshots)
    text = {item["id"]: "\n".join(wm._claude_login_lines(item, peers, 100)) for item in snapshots}
    check("two workers on the shared login are told so", "shares its Claude login with w3" in text["w1"], True)
    check("...in both directions", "shares its Claude login with w1" in text["w3"], True)
    check("renewing the operator's own login is flagged", "operator's own login" in text["w1"], True)
    check("a stopped worker is not counted as a peer", "w5" in text["w1"], False)
    check("a non-Claude worker draws no warning", text["g"], "")
    check("a worker on its own login draws no warning", "warning" in text["own1"], False)
    check("its own login is shown with its expiry", "logged in until" in text["own1"], True)
    check("an unused login directory says how to log in", "NOT LOGGED IN" in text["own2"], True)
    check("a login about to lapse is flagged", "login expires" in text["own3"], True)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# --- `workers login`: the session runs in the worker's directory, never ~ ------------------------------
# The session asks the operator to trust its working directory, so it must be the worker's own checkout
# (or the worker's install), not their home or wherever the command happened to be started.
tmp = Path(tempfile.mkdtemp())
with_checkout, without = f"login-cwd-test-{os.getpid()}-a", f"login-cwd-test-{os.getpid()}-b"
checkout = tc.HERE / "checkouts" / with_checkout / "EpsilonEridani"
saved_which, saved_run, saved_cwd, saved_home = shutil.which, subprocess.run, os.getcwd(), os.environ.get("HOME")
try:
    os.environ["HOME"] = str(tmp / "home")  # the sections above left $HOME on a deleted isolated home
    (tmp / "home").mkdir()
    (checkout / ".git").mkdir(parents=True)
    config = tmp / "workers.toml"
    config.write_text(
        "version = 1\n"
        + "".join(
            f'\n[[workers]]\nid = "{wid}"\nagent = "claude"\nclaude_config_dir = "{tmp / wid}"\n'
            for wid in (with_checkout, without)
        )
    )
    sessions = []

    def fake_claude(argv, env, cwd):
        sessions.append(Path(cwd))
        login(Path(env["CLAUDE_CONFIG_DIR"]), "T")  # what a finished /login leaves behind
        return subprocess.CompletedProcess(argv, 0)

    shutil.which = lambda name: "/usr/bin/claude" if name == "claude" else saved_which(name)
    subprocess.run = fake_claude
    os.chdir(Path.home())  # started from ~, which the session must not inherit
    check("login succeeds for a worker with a checkout", wm.claude_login(config, with_checkout), 0)
    check("login succeeds for a worker without one", wm.claude_login(config, without), 0)
    check("...the first session runs in the worker's checkout", sessions[0], checkout)
    check("...the second in the worker's install directory", sessions[1], tc.HERE)
    check("...and neither in ~", Path.home() in sessions, False)
finally:
    shutil.which, subprocess.run = saved_which, saved_run
    os.chdir(saved_cwd)
    if saved_home is not None:
        os.environ["HOME"] = saved_home
    shutil.rmtree(tc.HERE / "checkouts" / with_checkout, ignore_errors=True)
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
