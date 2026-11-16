#!/usr/bin/env python3
"""A PR round interrupted before it pushed keeps its work for the PR's next round, on any worker.

The next round of any kind used to throw that work away: prepare_checkout forces the checkout to main
and cleans it, and `gh pr checkout --force` resets the PR branch to its remote head. Between
2026-11-08 and 11-16, 30 never-pushed commits were found in the fleet's reflogs. Pinned, with real git
repositories (an "origin", and two workers' checkouts):

  1. A round that exited 0 leaves nothing to salvage, even with local commits (it chose not to push).
  2. An interrupted round (no exit recorded, as after the round timeout) keeps its commits AND its
     uncommitted edits, before the checkout is reset.
  3. Another worker's next round on the PR, at the same head, gets both back: the commits as commits,
     the edits as edits, and a note for the agent naming them. The salvage is consumed.
  4. If the PR moved in between, nothing is replayed and the salvage is set aside, not lost.
  5. A checkout that is not on the round's PR branch any more salvages nothing.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import epsiloneridani_worker as tc  # noqa: E402

salvage = tc.salvage
fails = 0


def check(name, got, expect):
    global fails
    ok = got == expect
    print(f"[{'OK ' if ok else 'XX '}] {name}: {got!r}")
    if not ok:
        print(f"      expected: {expect!r}")
        fails += 1


def git(cwd, *args):
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr}")
    return p.stdout.strip()


GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}

root = Path(tempfile.mkdtemp(prefix="epsiloneridani-salvage-"))
saved = {k: os.environ.get(k) for k in (salvage.SALVAGE_DIR_ENV, *GIT_ENV)}
try:
    os.environ.update(GIT_ENV)
    os.environ[salvage.SALVAGE_DIR_ENV] = str(root / "salvage")

    origin = root / "origin.git"
    seed = root / "seed"
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    (seed / "A.lean").write_text("theorem a : True := trivial\n")
    (seed / ".gitignore").write_text(".lake/\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "main")
    git(seed, "checkout", "-qb", "pr-branch")
    (seed / "A.lean").write_text("theorem a : True := trivial\ntheorem b : True := trivial\n")
    git(seed, "commit", "-qam", "the PR")
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(origin)], check=True)
    pr_head = git(seed, "rev-parse", "HEAD")

    def worker(name):
        co = root / name / "EpsilonEridani"
        subprocess.run(["git", "clone", "-q", str(origin), str(co)], check=True)
        state = root / name / "state"
        state.mkdir(parents=True)
        return SimpleNamespace(wid=name, state=state, checkout=co)

    def take_pr(cfg):
        """What a PR round does to the checkout before the agent runs."""
        git(cfg.checkout, "fetch", "-q", "origin")
        git(cfg.checkout, "checkout", "-q", "-f", "-B", "pr-branch", "origin/pr-branch")
        return git(cfg.checkout, "rev-parse", "HEAD")

    def reset_like_next_round(cfg):
        salvage.capture(cfg)
        git(cfg.checkout, "checkout", "-q", "-f", "-B", "main", "origin/main")
        git(cfg.checkout, "clean", "-fdxq", "-e", ".lake")

    kw = {"owner": "eic", "repo": "EpsilonEridani", "pr": 7}
    w27, w28 = worker("worker27"), worker("worker28")
    bundle = root / "salvage" / "eic__EpsilonEridani__7.bundle"

    print("1) a round that exited 0")
    base = take_pr(w27)
    salvage.note_start(w27, stage="fix", base=base, **kw)
    (w27.checkout / "A.lean").write_text("theorem a : True := trivial\n-- kept local on purpose\n")
    git(w27.checkout, "commit", "-qam", "local only")
    salvage.note_end(w27, 0)
    reset_like_next_round(w27)
    check("nothing salvaged", bundle.exists(), False)
    check("the round record is consumed", (w27.state / salvage.INTENT_FILE).exists(), False)

    print("2) an interrupted round")
    base = take_pr(w27)
    check("the round starts from the PR head", base, pr_head)
    salvage.note_start(w27, stage="fix", base=base, **kw)
    (w27.checkout / "A.lean").write_text("theorem a : True := trivial\ntheorem b' : True := trivial\n")
    git(w27.checkout, "commit", "-qam", "fix: address the naming finding")
    (w27.checkout / "B.lean").write_text("theorem c : True := trivial\n")  # an untracked new file
    (w27.checkout / "A.lean").write_text("theorem a : True := trivial\ntheorem b' : True := by trivial\n")
    (w27.checkout / ".lake").mkdir()
    (w27.checkout / ".lake" / "big.olean").write_text("build output")
    # no note_end: the round was torn down by the timeout
    reset_like_next_round(w27)
    check("the work is bundled", bundle.exists(), True)
    check("the checkout is clean on main again", git(w27.checkout, "status", "--porcelain"), "")
    meta = salvage._read_json(root / "salvage" / "eic__EpsilonEridani__7.json")
    check("...recording the base", meta["base"], pr_head)
    check("...and that edits were included", meta["wip"], True)

    print("3) another worker resumes it")
    head = take_pr(w28)
    note = salvage.restore(w28, w28.checkout, head=head, **kw)
    check("a note for the agent", bool(note) and "fix: address the naming finding" in note, True)
    check("...naming the edits", "uncommitted edits" in (note or ""), True)
    check(
        "the commit is back as a commit",
        git(w28.checkout, "log", "-1", "--format=%s"),
        "fix: address the naming finding",
    )
    check("...on top of the PR head", git(w28.checkout, "rev-parse", "HEAD~1"), pr_head)
    check(
        "the edits are back as edits",
        sorted(x.strip() for x in git(w28.checkout, "status", "--porcelain").splitlines()),
        ["?? B.lean", "M A.lean"],
    )
    check("...with their content", (w28.checkout / "A.lean").read_text().endswith("by trivial\n"), True)
    check(".lake was never bundled", (w28.checkout / ".lake").exists(), False)
    check("the salvage is consumed", bundle.exists(), False)
    check("...and kept for reference", any((root / "salvage" / "restored").glob("*.bundle")), True)
    check("a second restore finds nothing", salvage.restore(w28, w28.checkout, head=head, **kw), None)

    print("4) the PR moved in between")
    git(w28.checkout, "checkout", "-q", "-f", "-B", "main", "origin/main")
    base = take_pr(w27)
    salvage.note_start(w27, stage="fix-ci", base=base, **kw)
    (w27.checkout / "A.lean").write_text("theorem a : True := trivial\n-- wip\n")
    git(w27.checkout, "commit", "-qam", "wip")
    salvage.note_end(w27, 1)  # exited 1: the 5-hour limit
    reset_like_next_round(w27)
    check("captured", bundle.exists(), True)
    (seed / "A.lean").write_text("theorem a : True := trivial\ntheorem b : True := rfl.mpr trivial\n")
    git(seed, "commit", "-qam", "someone else pushed")
    git(seed, "push", "-q", str(origin), "pr-branch")
    head = take_pr(w28)
    check("nothing replayed onto a moved PR", salvage.restore(w28, w28.checkout, head=head, **kw), None)
    check("...the checkout stays at the new head", git(w28.checkout, "rev-parse", "HEAD"), head)
    check("...and the salvage is set aside", any((root / "salvage" / "superseded").glob("*.bundle")), True)

    print("5) a checkout no longer on the PR branch")
    base = take_pr(w27)
    salvage.note_start(w27, stage="fix", base=base, **kw)
    git(w27.checkout, "checkout", "-q", "-f", "-B", "main", "origin/main")
    reset_like_next_round(w27)
    check("nothing salvaged", bundle.exists(), False)
finally:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(root, ignore_errors=True)

print(f"\n{fails} failure(s)")
sys.exit(1 if fails else 0)
