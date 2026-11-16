"""Keep the work of a PR round that was interrupted before it pushed, and hand it to the next round.

A fix, fix-ci, rebase, bump or lint-repair round commits and pushes from inside the agent. When the
agent dies first (the 5-hour limit, the round timeout, a stopped service), its commits and edits stay
in the worker's checkout, and the next round of ANY kind threw them away: `prepare_checkout` forces
the checkout back to main and cleans it, and `gh pr checkout --force` resets the PR branch to its
remote head. Between 2026-11-08 and 11-16, 30 commits that never reached GitHub were found in the
fleet's reflogs this way, most from rounds cut off by the 5-hour limit just after their pre-push
local review asked for more work.

So a round records what it started from (`note_start`) and how it ended (`note_end`). The next
round's `prepare_checkout` calls `capture` before it touches anything: when the previous round did
not end with exit 0, its commits on top of the PR head, plus any uncommitted edits as one WIP commit,
are written to a git bundle in a directory every worker of the install shares. The next PR round on
that PR, on any worker, calls `restore` after checking the PR out: if the PR head is still the one the
work was built on, the work is replayed into the checkout (the WIP commit undone again, so the edits
are back as edits) and the agent is told what it is resuming. If the PR moved, the work is set aside,
not replayed onto a head it was not written for.

Everything here is best effort and never fails a round: a capture or restore that cannot be done is
logged and skipped, which is exactly the behaviour before this module existed."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from .config import Config, log
from .paths import HERE

SALVAGE_DIR_ENV = "EPSILONERIDANI_SALVAGE_DIR"
INTENT_FILE = "round-intent.json"
# A salvage older than this is set aside rather than replayed: by then the review it answered is
# likely superseded, and an agent told to resume it would be working from stale instructions.
SALVAGE_MAX_AGE_S = 7 * 86400
_SALVAGE_REF = "refs/epsiloneridani/salvage"


def salvage_root() -> Path:
    """Where interrupted rounds' work is kept: shared by every worker of the install, so a PR's next
    round can resume it whichever worker takes it. $EPSILONERIDANI_SALVAGE_DIR overrides."""
    override = os.environ.get(SALVAGE_DIR_ENV, "").strip()
    return Path(override).expanduser() if override else HERE / "state" / "salvage"


def _key(owner: str, repo: str, pr: int) -> str:
    return f"{owner}__{repo}__{pr}"


def _git(co: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(co), *args], capture_output=True, text=True, env=env, timeout=300)


def _intent_path(cfg: Config) -> Path | None:
    """The worker's round record, or None for a configuration without a state directory."""
    state = getattr(cfg, "state", None)
    return None if state is None else Path(state) / INTENT_FILE


def _current_branch(co: Path) -> str | None:
    """The checked-out branch's name, or None (detached HEAD, or no repository)."""
    try:
        name = _git(co, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return name or None


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def note_start(cfg: Config, *, stage: str, owner: str, repo: str, pr: int, base: str) -> None:
    """Record that a PR round is about to run its agent on `base` (the PR head it checked out)."""
    path = _intent_path(cfg)
    if path is None:
        return
    try:
        _write_json(
            path,
            {
                "stage": stage,
                "owner": owner,
                "repo": repo,
                "pr": pr,
                "base": base,
                # The branch too: `capture` must not mistake another branch that merely contains `base`
                # (main, after the PR merged) for this round's work.
                "branch": _current_branch(Path(cfg.checkout)),
                "worker": cfg.wid,
                "started": time.time(),
                "rc": None,
            },
        )
    except OSError:
        pass


def note_end(cfg: Config, rc: int | None) -> None:
    """Record how the round's agent ended. Exit 0 means it finished on its own terms, pushed or
    deliberately not, so nothing is salvaged from it; anything else, or no record at all (a round
    torn down by the timeout never gets here), is an interruption."""
    path = _intent_path(cfg)
    data = None if path is None else _read_json(path)
    if path is None or data is None:
        return
    data["rc"] = rc
    try:
        _write_json(path, data)
    except OSError:
        pass


def capture(cfg: Config) -> None:
    """Before the checkout is reset: keep what an interrupted previous round left on its PR branch."""
    path = _intent_path(cfg)
    if path is None:
        return
    intent = _read_json(path)
    try:
        path.unlink(missing_ok=True)  # one look per round record, whatever happens below
    except OSError:
        pass
    if intent is None or intent.get("rc") == 0:
        return
    co = cfg.checkout
    try:
        _capture(cfg, co, intent)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as e:
        log(f"salvage: could not keep the interrupted round's work ({e}) — it will be discarded")


def _capture(cfg: Config, co: Path, intent: dict) -> None:
    base, pr = str(intent["base"]), int(intent["pr"])
    owner, repo = str(intent["owner"]), str(intent["repo"])
    if not (co / ".git").is_dir():
        return
    head = _git(co, "rev-parse", "HEAD").stdout.strip()
    if not head or _git(co, "merge-base", "--is-ancestor", base, head).returncode:
        return  # the checkout is not on the round's PR branch any more: nothing of it to keep
    branch = intent.get("branch")
    if branch and _current_branch(co) != branch:
        return  # another branch that happens to contain the base (main, after a merge) is not this work
    tip, wip = head, False
    if _git(co, "status", "--porcelain").stdout.strip():
        # The edits as one commit, without touching the checkout's own index or files: a scratch
        # index takes the whole tree but .lake, and commit-tree hangs it on HEAD.
        index = Path(cfg.state) / ".salvage-index"
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}
        try:
            _git(co, "read-tree", "HEAD", env=env)
            # .lake explicitly, not only through the repository's .gitignore: a build tree in the
            # bundle would make it huge, and the next round rebuilds it anyway.
            # (An exclude pathspec would not do: `git add` fails outright when it names an ignored path.)
            added = _git(co, "add", "-A", env=env).returncode == 0
            if added and _git(co, "rm", "-r", "-q", "--cached", "--ignore-unmatch", ".lake", env=env).returncode == 0:
                tree = _git(co, "write-tree", env=env).stdout.strip()
                made = _git(
                    co,
                    "-c",
                    "user.name=epsiloneridani",
                    "-c",
                    "user.email=epsiloneridani@localhost",
                    "commit-tree",
                    tree,
                    "-p",
                    head,
                    "-m",
                    "WIP: uncommitted edits of an interrupted round (salvaged)",
                ).stdout.strip()
                if made:
                    tip, wip = made, True
        finally:
            index.unlink(missing_ok=True)
    if tip == base:
        return
    subjects = _git(co, "log", "--format=%s", f"{base}..{tip}").stdout.splitlines()
    root = salvage_root()
    key = _key(owner, repo, pr)
    root.mkdir(parents=True, exist_ok=True)
    if _git(co, "update-ref", _SALVAGE_REF, tip).returncode:
        return
    bundle = root / f"{key}.bundle"
    tmp = root / f".{key}.{os.getpid()}.bundle"
    made = _git(co, "bundle", "create", str(tmp), _SALVAGE_REF, f"^{base}")
    _git(co, "update-ref", "-d", _SALVAGE_REF)
    if made.returncode:
        tmp.unlink(missing_ok=True)
        log(f"salvage: bundling #{pr}'s unpushed work failed: {made.stderr.strip()[-200:]}")
        return
    os.replace(tmp, bundle)
    _write_json(
        root / f"{key}.json",
        {
            "owner": owner,
            "repo": repo,
            "pr": pr,
            "base": base,
            "tip": tip,
            "wip": wip,
            "subjects": subjects,
            "stage": intent.get("stage"),
            "worker": intent.get("worker"),
            "started": intent.get("started"),
            "rc": intent.get("rc"),
            "captured": time.time(),
        },
    )
    n = len(subjects) - (1 if wip else 0)
    what = f"{n} unpushed commit(s)" + (" and uncommitted edits" if wip else "")
    log(f"salvage: kept {what} from the interrupted {intent.get('stage')} round on #{pr} (base {base[:12]})")


def _retire(root: Path, key: str, why: str) -> None:
    """Move a salvage out of the way, keeping it for a person to look at."""
    dest = root / why
    dest.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for ext in (".json", ".bundle"):
        src = root / f"{key}{ext}"
        if src.exists():
            os.replace(src, dest / f"{key}-{stamp}{ext}")


def restore(cfg: Config, co: Path, *, owner: str, repo: str, pr: int, head: str) -> str | None:
    """Replay an interrupted round's work for this PR into the freshly checked-out PR branch, when it
    was built on `head`. Returns the note to give the agent, or None when there is nothing to resume."""
    root = salvage_root()
    key = _key(owner, repo, pr)
    meta = _read_json(root / f"{key}.json")
    if meta is None or not (root / f"{key}.bundle").exists():
        return None
    try:
        return _restore(co, root, key, meta, pr, head)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as e:
        log(f"salvage: could not restore #{pr}'s interrupted work ({e}) — continuing from the PR head")
        _git(co, "reset", "-q", "--hard", head)
        return None


def _restore(co: Path, root: Path, key: str, meta: dict, pr: int, head: str) -> str | None:
    base, tip = str(meta["base"]), str(meta["tip"])
    if base != head:
        log(f"salvage: #{pr} moved from {base[:12]} to {head[:12]} since the interrupted round — set aside")
        _retire(root, key, "superseded")
        return None
    captured = meta.get("captured")
    if not isinstance(captured, (int, float)) or time.time() - captured > SALVAGE_MAX_AGE_S:
        _retire(root, key, "expired")
        return None
    got = _git(co, "fetch", "-q", "--no-tags", str(root / f"{key}.bundle"), _SALVAGE_REF)
    if got.returncode or _git(co, "rev-parse", "FETCH_HEAD").stdout.strip() != tip:
        log(f"salvage: #{pr}'s bundle did not yield {tip[:12]} — set aside")
        _retire(root, key, "unreadable")
        return None
    if _git(co, "reset", "-q", "--hard", tip).returncode:
        return None
    wip = bool(meta.get("wip"))
    if wip and _git(co, "reset", "-q", "HEAD~1").returncode:  # the edits back as edits, unstaged
        _git(co, "reset", "-q", "--hard", head)
        return None
    _retire(root, key, "restored")
    subjects = [s for s in meta.get("subjects") or [] if isinstance(s, str)]
    if wip:
        subjects = subjects[1:]  # git log lists the WIP commit first
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(captured))
    how = "timed out or was stopped" if meta.get("rc") is None else f"exited with status {meta.get('rc')}"
    lines = [
        "",
        "## Resuming an interrupted round",
        "",
        f"A previous {meta.get('stage') or 'PR'} round on this PR ({meta.get('worker') or 'a worker'}, "
        f"{when}) {how} before it pushed. Its work has been restored into this checkout on top of the "
        f"PR head `{head[:12]}`:",
        "",
    ]
    lines += [f"- commit: {s}" for s in subjects] or ["- (no commits)"]
    if wip:
        lines.append("- uncommitted edits, now back in the working tree")
    lines += [
        "",
        "None of it is verified or pushed. Start by reading it (`git log` and `git diff` against "
        f"`{head[:12]}`). Keep what is right, finish the job, verify it as the rest of these instructions "
        f"require, and push. If it is wrong, discard it with `git reset --hard {head[:12]}` and start over.",
        "",
    ]
    log(
        f"salvage: restored the interrupted round's work on #{pr} ({len(subjects)} commit(s)"
        + (", edits)" if wip else ")")
    )
    return "\n".join(lines)
