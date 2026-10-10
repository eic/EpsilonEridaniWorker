#!/usr/bin/env python3
"""The PyPI update hint: `workers status` and the dashboard say when a newer epsiloneridani is
published, and which live workers still run older code than the one installed. Never installs.

Properties under test:
  - only final releases count: pre-releases, dev builds and fully yanked releases are never suggested;
  - the PyPI answer is cached for a day, a failed lookup for a few hours, and a failure never raises;
  - the notice appears only when PyPI is strictly ahead, names both versions and an upgrade command
    that fits how this copy was installed (pip, uv tool, pipx, VCS; none for an editable checkout);
  - $EPSILONERIDANI_NO_UPDATE_CHECK=1 and a bare source tree (nothing installed) both mean no check;
  - a live worker launched on an older version than the installed one is flagged restart-pending;
  - `workers status` prints the update line and the per-worker hint;
  - `workers restart --all [--after-round]` targets every enabled worker, and ids and --all exclude.

Dependency-free; no network (every fetch is a stub).
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

root = Path(tempfile.mkdtemp(prefix="epsiloneridani-update-test-"))
for key in ("EPSILONERIDANI_CONFIG_HOME", "EPSILONERIDANI_WORKERS_CONFIG", "EPSILONERIDANI_WORKERS_STATE_DIR"):
    os.environ.pop(key, None)
os.environ["XDG_CONFIG_HOME"] = str(root / "config")
os.environ["XDG_STATE_HOME"] = str(root / "state")
os.environ["EPSILONERIDANI_RUNTIME_DIR"] = str(root / "run")
os.environ.pop("EPSILONERIDANI_NO_UPDATE_CHECK", None)

import epsiloneridani_worker.update_check as uc  # noqa: E402
import epsiloneridani_worker.worker_manager as wm  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")
    fails += not ok


# --- versions ---------------------------------------------------------------------------------------
check("final release key", uc.version_key("0.16.0"), (0, 16))
check("1.0 == 1.0.0", uc.version_key("1.0") == uc.version_key("1.0.0"), True)
check("0.10.0 > 0.9.3", uc.version_key("0.10.0") > uc.version_key("0.9.3"), True)
for v in ("0.17.0rc1", "0.17.0.dev3", "0.16.0+local", "", "junk"):
    check(f"not a final release: {v!r}", uc.version_key(v), None)

f = [{"yanked": False}]
payload = {
    "releases": {
        "0.15.0": f,
        "0.16.0": f,
        "0.17.0rc1": f,  # pre-release: skipped
        "0.18.0": [{"yanked": True}, {"yanked": True}],  # every file yanked: skipped
        "0.19.0": [],  # no files: skipped
    }
}
check("newest final, non-yanked release", uc.latest_from_pypi(payload), "0.16.0")
check("malformed payload", uc.latest_from_pypi({"releases": []}), None)

# --- the cache ---------------------------------------------------------------------------------------
cache = root / "cache" / "update-check.json"
calls = []


def fetch_ok(timeout):
    calls.append(timeout)
    return "0.17.0"


def fetch_fail(timeout):
    calls.append(timeout)
    raise OSError("network unreachable")


T0 = 1_000_000.0
check("first lookup asks PyPI", uc.latest_version(cache, now=T0, fetch=fetch_ok), "0.17.0")
check("and caches the answer", json.loads(cache.read_text())["latest"], "0.17.0")
check("a fresh cache is reused", (uc.latest_version(cache, now=T0 + 3600, fetch=fetch_fail), len(calls)), ("0.17.0", 1))
check(
    "a day later it asks again; a failure is None, not an exception",
    uc.latest_version(cache, now=T0 + uc.CHECK_INTERVAL_S + 1, fetch=fetch_fail),
    None,
)
check("the failure is remembered", "error" in json.loads(cache.read_text()), True)
n = len(calls)
uc.latest_version(cache, now=T0 + uc.CHECK_INTERVAL_S + 60, fetch=fetch_ok)
check("no retry inside the failure window", len(calls), n)
check(
    "retried once the failure window passes",
    uc.latest_version(cache, now=T0 + uc.CHECK_INTERVAL_S + uc.FAILURE_RETRY_S + 2, fetch=fetch_ok),
    "0.17.0",
)
cache.write_text("{not json")
check("a corrupt cache is re-fetched", uc.latest_version(cache, now=T0, fetch=fetch_ok), "0.17.0")

# --- install kind and upgrade command ----------------------------------------------------------------
real_direct_url = uc._direct_url
uc._direct_url = lambda: {"dir_info": {"editable": True}}
check("editable install", uc.install_kind("/x/venv"), "editable")
uc._direct_url = lambda: {"vcs_info": {"vcs": "git"}}
check("VCS install", uc.install_kind("/x/venv"), "vcs")
uc._direct_url = lambda: {}
check("uv tool", uc.install_kind("/home/u/.local/share/uv/tools/epsiloneridani"), "uv-tool")
check("pipx", uc.install_kind("/home/u/.local/pipx/venvs/epsiloneridani"), "pipx")
check("plain venv", uc.install_kind("/project/venv"), "pip")
uc._direct_url = real_direct_url
check("pip command", uc.upgrade_command("pip", "/v/bin/python"), "/v/bin/python -m pip install -U epsiloneridani")
check(
    "VCS installs move to the release",
    uc.upgrade_command("vcs", "/v/bin/python"),
    "/v/bin/python -m pip install -U epsiloneridani",
)
check("uv tool command", uc.upgrade_command("uv-tool"), "uv tool upgrade epsiloneridani")
check("pipx command", uc.upgrade_command("pipx"), "pipx upgrade epsiloneridani")
check("editable: no package command", uc.upgrade_command("editable"), None)

# --- the notice ----------------------------------------------------------------------------------------
real_installed = uc.installed_version
real_kind = uc.install_kind


def notice(installed, latest, now=T0):
    uc.installed_version = lambda: installed
    uc.install_kind = lambda prefix=None: "pip"
    cache.unlink(missing_ok=True)
    try:
        return uc.update_available(cache, now=now, fetch=lambda timeout: latest)
    finally:
        uc.installed_version, uc.install_kind = real_installed, real_kind


info = notice("0.16.0", "0.17.0")
check("newer on PyPI: a notice", (info.installed, info.latest) if info else None, ("0.16.0", "0.17.0"))
check("it names both versions", "0.17.0 is available (installed 0.16.0)" in info.message(), True)
check("and the graceful restart", "epsiloneridani workers restart --all --after-round" in info.message(), True)
check("same version: none", notice("0.16.0", "0.16.0"), None)
check("installed is ahead: none", notice("0.17.0", "0.16.0"), None)
check("PyPI did not answer: none", notice("0.16.0", None), None)
check("a dev build of X is told about the final X", notice("0.16.1.dev3+g1234", "0.16.1") is not None, True)
check("a dev build ahead of PyPI: none", notice("0.17.0.dev3", "0.16.0"), None)
check("a post-release of X: none for X", notice("0.16.0.post1", "0.16.0"), None)
check("not installed (a bare source tree): none", notice(None, "0.17.0"), None)
os.environ["EPSILONERIDANI_NO_UPDATE_CHECK"] = "1"
check("disabled by env: none", notice("0.16.0", "0.17.0"), None)
os.environ.pop("EPSILONERIDANI_NO_UPDATE_CHECK", None)
editable = uc.UpdateInfo("0.16.0", "0.17.0", None)
check("editable notice says to update the checkout", "update your checkout" in editable.message(), True)

# --- restart pending -----------------------------------------------------------------------------------
check("older running than installed", uc.restart_pending("0.16.0", "0.17.0"), True)
check("same version", uc.restart_pending("0.17.0", "0.17.0"), False)
check("unknown running version (launched before this was recorded)", uc.restart_pending(None, "0.17.0"), False)

# --- workers status -------------------------------------------------------------------------------------
config = root / "config" / "workers.toml"
config.parent.mkdir(parents=True, exist_ok=True)
config.write_text(
    'version = 1\n\n[[workers]]\nid = "a"\nenabled = true\n\n[[workers]]\nid = "b"\nenabled = true\n\n'
    '[[workers]]\nid = "c"\nenabled = false\n'
)
specs = wm.load_worker_specs(config)
item = {
    "id": "a",
    "actual": "waiting-quota",
    "desired": "running",
    "alive": True,
    "version": "0.16.0",
    "installed_version": "0.17.0",
    "restart_pending": True,
    "agent": "claude",
    "only": [],
    "sandbox": "host",
    "spec": specs[0].as_dict(),
}
lines = wm._worker_status_lines(config, [item], True, width=200, update=info.message())
check("status prints the update line", any(line.startswith("update:  epsiloneridani 0.17.0") for line in lines), True)
check(
    "and flags the worker still on older code",
    any(line.startswith("a — ") and "running 0.16.0; restart to pick up 0.17.0" in line for line in lines),
    True,
)
lines = wm._worker_status_lines(config, [dict(item, restart_pending=False)], True, width=200)
check(
    "no update and nothing pending: neither line",
    any("update:" in x or "restart to pick up" in x for x in lines),
    False,
)

# The snapshot judges restart-pending from the version recorded at launch.
state = wm.workers_state_dir()
state.mkdir(parents=True, exist_ok=True)
(state / "a.json").write_text(
    json.dumps(
        {
            "id": "a",
            "alive": True,
            "state": "running",
            "version": "0.0.1",
            "pid": os.getpid(),
            "wrapper_pid": os.getpid(),
            "host": wm.this_host(),
            "heartbeat_at": time.time(),
            "spec_hash": specs[0].fingerprint(),
        }
    )
)
real_wm_installed = wm.installed_version
real_runner = wm.runner_status
wm.installed_version = lambda: "0.17.0"
wm.runner_status = lambda wid: (
    json.loads((state / f"{wid}.json").read_text()) if (state / f"{wid}.json").exists() else {}
)
snap = {s["id"]: s for s in wm.worker_snapshots(specs)}
check("snapshot: live worker on 0.0.1, 0.17.0 installed", snap["a"]["restart_pending"], True)
check("snapshot: a worker that is not running is never pending", snap["b"]["restart_pending"], False)
wm.installed_version, wm.runner_status = real_wm_installed, real_runner

# --- restart --all ---------------------------------------------------------------------------------------
drained, restarted = [], []
wm.request_drain = lambda wid, restart: drained.append((wid, restart))
wm.ensure_manager = lambda cfg: None
wm.restart_worker = lambda cfg, wid: restarted.append(wid)


def run(**kw):
    """cmd_workers' return code, or the exit code a refused request dies with."""
    args = SimpleNamespace(
        workers_action="restart", config=config, worker_id=None, all_workers=False, after_round=False
    )
    for k, v in kw.items():
        setattr(args, k, v)
    try:
        return wm.cmd_workers(args)
    except SystemExit as exc:
        return "refused" if exc.code else 0


check(
    "restart --all --after-round: every enabled worker, gracefully",
    (run(all_workers=True, after_round=True), drained),
    (0, [("a", True), ("b", True)]),
)
check("restart --all: every enabled worker, at once", (run(all_workers=True), restarted), (0, ["a", "b"]))
drained.clear()
check("restart ID --after-round still works", (run(worker_id="b", after_round=True), drained), (0, [("b", True)]))
check("an id and --all together is refused", run(worker_id="a", all_workers=True), "refused")
check("neither an id nor --all is refused", run(), "refused")

print(f"\n{'PASS' if fails == 0 else f'FAIL ({fails})'}")
sys.exit(1 if fails else 0)
