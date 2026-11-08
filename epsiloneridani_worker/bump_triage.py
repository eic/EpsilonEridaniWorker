"""Tell a bump PR that EpsilonEridani can adapt from one that only its dependencies can.

A bump PR goes red for one of two reasons. Either the new pins changed something EpsilonEridani/ uses (a
renamed lemma, a moved module: adapt it, which is what a bump round is for), or a DEPENDENCY itself
no longer builds at the new pins. On 2026-11-08 PR #163 paired Physlib with a mathlib past a refactor
Physlib had not caught up with; only Physlib modules failed, and three Opus bump rounds spent on it
could change nothing. This reads the failing build's log and answers which case it is.

Conservative on purpose: it answers "dependency only" just when at least one module failed and every
failing module is outside EpsilonEridani. Anything it cannot read or parse leaves the round to run as before.
"""

from __future__ import annotations

import json
import re

from .github import gh_run

OWN_PREFIX = "EpsilonEridani"
# Lake's failure lines: `✖ [9292/9383] Building Physlib.Relativity.Tensors.RealTensor.Velocity.Basic (5.7s)`
# and `✖ [9264/9358] Running Physlib.Relativity.Tensors...` (a missing file, a bad import).
# The module is whatever token Lake prints there, up to the next whitespace (before the timing), with
# a «guillemet-quoted» component kept whole even if it contains spaces: Lean names may start with
# non-ASCII letters, so no identifier syntax is assumed.
_FAILED = re.compile(r"✖ \[\d+/\d+\] (?:Building|Running|Compiling) ((?:«[^»]*»|[^\s«])+)")
_RUN_ID = re.compile(r"/actions/runs/(\d+)")


def failing_modules(log_text: str) -> list[str]:
    """The modules Lake reported as failed, in order, without repeats."""
    seen: dict[str, None] = {}
    for m in _FAILED.finditer(log_text or ""):
        seen.setdefault(m.group(1), None)
    return list(seen)


def dependency_only(modules: list[str]) -> bool:
    """True when something failed and nothing that failed is EpsilonEridani's own."""
    return bool(modules) and not any(m == OWN_PREFIX or m.startswith(OWN_PREFIX + ".") for m in modules)


def build_run_id(statuses: list[dict]) -> str | None:
    """The workflow run behind the newest `build` commit status (the trusted sandboxed build), when
    that status is a failure or an error: a green or pending head has no failure to triage, and no
    log is worth fetching for it."""
    for s in statuses or []:  # the REST API lists a commit's statuses newest first
        if isinstance(s, dict) and s.get("context") == "build":
            if s.get("state") not in ("failure", "error"):
                return None
            m = _RUN_ID.search(s.get("target_url") or "")
            return m.group(1) if m else None
    return None


def dependency_failure(repo: str, head: str) -> list[str] | None:
    """The failing modules when `head`'s build failed only inside dependencies, else None (EpsilonEridani
    code failed too, nothing parsed, or GitHub could not be read)."""
    st = gh_run(["gh", "api", f"repos/{repo}/commits/{head}/statuses?per_page=100"])
    if st.returncode != 0:
        return None
    try:
        run_id = build_run_id(json.loads(st.stdout or "[]"))
    except json.JSONDecodeError:
        return None
    if not run_id:
        return None
    log = gh_run(["gh", "run", "view", run_id, "--repo", repo, "--log-failed"])
    if log.returncode != 0:
        return None
    modules = failing_modules(log.stdout)
    return modules if dependency_only(modules) else None
