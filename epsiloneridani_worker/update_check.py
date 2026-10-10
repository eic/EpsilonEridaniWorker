"""Is a newer epsiloneridani on PyPI? A hint for the operator, never an action.

The interactive views (`workers status`, the dashboard) ask `update_notice`, which compares the
installed version with the newest final release on PyPI and, when PyPI is ahead, returns one line
naming both versions and how to upgrade this install. Nothing here installs anything: an upgrade
changes the code that gates real model spend, so it stays the operator's decision.

The PyPI answer is cached for a day (a failed lookup for a few hours), so a `status --watch` or a
dashboard refreshing every two seconds makes at most one request a day, and an offline machine
stays quiet instead of retrying. A source tree that is not installed (the Docker image runs the
copied source) has no installed version and is never checked. $EPSILONERIDANI_NO_UPDATE_CHECK=1
turns the check off entirely.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
import re
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DIST = "epsiloneridani"
PYPI_URL = f"https://pypi.org/pypi/{DIST}/json"
CHECK_INTERVAL_S = 24 * 3600
FAILURE_RETRY_S = 6 * 3600
FETCH_TIMEOUT_S = 3.0
NO_CHECK_ENV = "EPSILONERIDANI_NO_UPDATE_CHECK"
CACHE_NAME = "update-check.json"

# A final release only (1.2 / 0.16.0): pre-releases, dev and local versions are never suggested.
_FINAL = re.compile(r"^\d+(?:\.\d+)*$")


def disabled() -> bool:
    return os.environ.get(NO_CHECK_ENV) == "1"


def version_key(version: str) -> tuple[int, ...] | None:
    """A comparable key for a FINAL release, or None for anything else (pre-release, dev, local, junk).
    Trailing zeros are dropped so 1.0 and 1.0.0 compare equal."""
    if not _FINAL.match(version or ""):
        return None
    parts = [int(p) for p in version.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def installed_version() -> str | None:
    """The version installed on disk NOW, or None when epsiloneridani is not installed (a bare source
    tree). Caches are invalidated first: a long-lived process (the manager, the dashboard) must see an
    upgrade that landed after it started, not the version it imported."""
    importlib.invalidate_caches()
    try:
        return importlib.metadata.version(DIST)
    except importlib.metadata.PackageNotFoundError:
        return None


def _direct_url() -> dict:
    try:
        raw = importlib.metadata.distribution(DIST).read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        return {}
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def install_kind(prefix: str | None = None) -> str:
    """How this copy was installed, which decides the upgrade command: editable | vcs | uv-tool |
    pipx | pip. PEP 610's direct_url.json says editable and VCS installs apart from index ones; the
    tool managers are recognised by the environment they install into."""
    info = _direct_url()
    if (info.get("dir_info") or {}).get("editable"):
        return "editable"
    if info.get("vcs_info"):
        return "vcs"
    prefix = (prefix or sys.prefix).replace("\\", "/")
    if "/uv/tools/" in prefix:
        return "uv-tool"
    if "/pipx/venvs/" in prefix:
        return "pipx"
    return "pip"


def upgrade_command(kind: str, python: str | None = None) -> str | None:
    """The command that upgrades this install from PyPI, or None when the right move is not a package
    command (an editable checkout is upgraded with git). A VCS install gets the pip command too: it
    moves the install onto the published release, which is what the notice is about."""
    if kind == "editable":
        return None
    if kind == "uv-tool":
        return f"uv tool upgrade {DIST}"
    if kind == "pipx":
        return f"pipx upgrade {DIST}"
    return f"{python or sys.executable} -m pip install -U {DIST}"


def latest_from_pypi(payload: dict) -> str | None:
    """The newest final release in a PyPI JSON response that still has a file not yanked."""
    best: tuple[tuple[int, ...], str] | None = None
    releases = payload.get("releases") if isinstance(payload, dict) else None
    if not isinstance(releases, dict):
        return None
    for version, files in releases.items():
        key = version_key(version)
        if key is None or not isinstance(files, list) or not files:
            continue
        if all(isinstance(f, dict) and f.get("yanked") for f in files):
            continue
        if best is None or key > best[0]:
            best = (key, version)
    return best[1] if best else None


def _fetch_latest(timeout: float) -> str | None:
    from .oauth import USER_AGENT  # the same honest agent string the token requests use

    req = urllib.request.Request(PYPI_URL, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return latest_from_pypi(json.load(resp))


def latest_version(cache: Path, *, now: float | None = None, fetch=None) -> str | None:
    """The newest final release on PyPI, from `cache` while it is fresh, else asked for (and cached).
    Never raises: an unreachable PyPI is None, remembered for FAILURE_RETRY_S so it is not retried on
    every refresh."""
    now = time.time() if now is None else now
    try:
        cached = json.loads(cache.read_text())
    except (OSError, ValueError):
        cached = {}
    if isinstance(cached, dict):
        checked = cached.get("checked_at")
        latest = cached.get("latest")
        ttl = CHECK_INTERVAL_S if latest else FAILURE_RETRY_S
        if isinstance(checked, (int, float)) and 0 <= now - checked < ttl:
            return latest if isinstance(latest, str) else None
    fetch = fetch or _fetch_latest
    record: dict = {"checked_at": now}
    try:
        latest = fetch(FETCH_TIMEOUT_S)
        record["latest"] = latest
    except Exception as exc:  # network, TLS, HTTP, JSON: all just mean "no answer today"
        latest = None
        record["error"] = f"{type(exc).__name__}: {exc}"[:200]
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_name(f".{cache.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(record))
        os.replace(tmp, cache)
    except OSError:
        pass
    return latest


@dataclass(frozen=True)
class UpdateInfo:
    installed: str
    latest: str
    command: str | None

    def message(self) -> str:
        how = (
            f"upgrade with `{self.command}`, then `epsiloneridani workers restart --all --after-round`"
            if self.command
            else "update your checkout, then `epsiloneridani workers restart --all --after-round`"
        )
        return f"epsiloneridani {self.latest} is available (installed {self.installed}): {how}"


def update_available(cache: Path, *, now: float | None = None, fetch=None) -> UpdateInfo | None:
    """An UpdateInfo when PyPI has a newer final release than the one installed, else None (also when
    the check is disabled, nothing is installed, or PyPI did not answer)."""
    if disabled():
        return None
    installed = installed_version()
    if installed is None:
        return None
    latest = latest_version(cache, now=now, fetch=fetch)
    have, want = version_key(installed), version_key(latest or "")
    if want is None:
        return None
    if have is None:
        # Not a final release: compare its release part. A dev or pre-release of X comes BEFORE X, so
        # the final X is an upgrade (0.16.1.dev3 -> 0.16.1); a post-release of X comes after it.
        m = re.match(r"^(\d+(?:\.\d+)*)", installed)
        have = version_key(m.group(1)) if m else None
        if have is None or want < have or (want == have and ".post" in installed):
            return None
    elif want <= have:
        return None
    return UpdateInfo(installed, latest, upgrade_command(install_kind()))


def restart_pending(running: str | None, installed: str | None) -> bool:
    """Whether a live worker started on an older version than the one now installed."""
    return bool(running and installed and running != installed)
