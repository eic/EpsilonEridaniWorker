#!/usr/bin/env python3
"""$EPSILONERIDANI_LAKE_ROOT puts each worker's `.lake` on a disk of the operator's choosing.

A `.lake` is hundreds of thousands of files. On a cluster whose shared filesystem has a per-user
file-count quota, a new worker's first clone built its `.lake` there (90k files in 15 minutes) and
filled the quota for every worker. The operator's workaround linked `.lake` to node-local scratch for
a fixed list of worker ids at service start, which no worker added later got. This harness pins:

  1. Unset, nothing changes: a real `.lake` stays where it is.
  2. Set, a checkout with no `.lake` gets a link to <root>/<worker id>/dot-lake, `/.lake` is added to
     .git/info/exclude (EpsilonEridani ignores `/.lake/`, which matches no symlink), and a second call
     is a no-op.
  3. A real `.lake` is moved into an empty target, contents kept, and replaced by the link.
  4. A real `.lake` with a non-empty target is left alone and the round refuses to build.
  5. A link the operator pointed elsewhere is kept.
  6. prepare_checkout links `.lake` on a fresh checkout before the clean, and the clean keeps it.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import epsiloneridani_worker as tc  # noqa: E402

agents = tc.agents
ENV = agents.LAKE_ROOT_ENV
fails = 0


def check(name, ok, detail=""):
    global fails
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + ("" if ok else f"  {detail}"))
    fails += not ok


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


def make_checkout(root: Path) -> Path:
    co = root / "checkouts" / "w1" / "EpsilonEridani"
    co.mkdir(parents=True)
    git(co, "init", "-q", "-b", "main")
    (co / ".gitignore").write_text("/.lake/\n")
    return co


def excluded(co: Path) -> bool:
    exclude = co / ".git" / "info" / "exclude"
    return exclude.is_file() and "/.lake" in exclude.read_text().splitlines()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="epsiloneridani-lake-root-"))
    root = tmp / "scratch"

    print("1) unset changes nothing")
    os.environ.pop(ENV, None)
    co = make_checkout(tmp / "a")
    (co / ".lake").mkdir()
    cfg = SimpleNamespace(wid="w1", checkout=co)
    check("returns True", agents.place_lake(cfg))
    check("the real .lake stays", (co / ".lake").is_dir() and not (co / ".lake").is_symlink())
    check("no root created", not root.exists())

    os.environ[ENV] = str(root)
    target = root / "w1" / "dot-lake"

    print("2) a fresh checkout gets the link")
    co = make_checkout(tmp / "b")
    cfg = SimpleNamespace(wid="w1", checkout=co)
    check("returns True", agents.place_lake(cfg))
    check(
        ".lake is a link to <root>/<id>/dot-lake",
        (co / ".lake").is_symlink() and os.path.realpath(co / ".lake") == os.path.realpath(target),
        os.readlink(co / ".lake") if (co / ".lake").is_symlink() else "not a link",
    )
    check("/.lake is excluded", excluded(co))
    check("git status shows no .lake", ".lake" not in git(co, "status", "--porcelain", "--ignored=no"))
    check("idempotent", agents.place_lake(cfg) and (co / ".lake").is_symlink())
    check("exclude line not repeated", (co / ".git" / "info" / "exclude").read_text().splitlines().count("/.lake") == 1)

    print("3) a real .lake moves into an empty target")
    co = make_checkout(tmp / "c")
    (co / ".lake" / "build").mkdir(parents=True)
    (co / ".lake" / "build" / "A.olean").write_text("olean")
    target = root / "w3" / "dot-lake"
    cfg = SimpleNamespace(wid="w3", checkout=co)
    check("returns True", agents.place_lake(cfg))
    check("now a link", (co / ".lake").is_symlink())
    check("contents kept in the target", (target / "build" / "A.olean").read_text() == "olean")
    check("readable through the link", (co / ".lake" / "build" / "A.olean").is_file())

    print("4) a real .lake with a non-empty target refuses")
    co = make_checkout(tmp / "d")
    (co / ".lake").mkdir()
    (co / ".lake" / "mine").write_text("x")
    target = root / "w4" / "dot-lake"
    target.mkdir(parents=True)
    (target / "theirs").write_text("y")
    cfg = SimpleNamespace(wid="w4", checkout=co)
    check("returns False", not agents.place_lake(cfg))
    check(".lake untouched", (co / ".lake" / "mine").is_file() and not (co / ".lake").is_symlink())
    check("target untouched", (target / "theirs").is_file() and not (target / "mine").exists())

    print("5) an operator's link elsewhere is kept")
    co = make_checkout(tmp / "e")
    elsewhere = tmp / "elsewhere"
    elsewhere.mkdir()
    (co / ".lake").symlink_to(elsewhere)
    cfg = SimpleNamespace(wid="w5", checkout=co)
    check("returns True", agents.place_lake(cfg))
    check("link unchanged", os.path.realpath(co / ".lake") == os.path.realpath(elsewhere))

    print("6) prepare_checkout links before the clean, and the clean keeps it")
    origin = tmp / "origin.git"
    seed = tmp / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    git(seed, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "base")
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(origin)], check=True)
    co = tmp / "f" / "checkouts" / "w6" / "EpsilonEridani"
    subprocess.run(["git", "clone", "-q", str(origin), str(co)], check=True)
    (co / "stray.txt").write_text("leftover")
    cfg = SimpleNamespace(wid="w6", checkout=co)
    saved = agents.sync_mathlib_pool, agents.clean_lake_cache_after_toolchain_bump
    agents.sync_mathlib_pool = lambda cfg: None
    agents.clean_lake_cache_after_toolchain_bump = lambda cfg: None
    try:
        ok = agents.prepare_checkout(cfg)
    finally:
        agents.sync_mathlib_pool, agents.clean_lake_cache_after_toolchain_bump = saved
    check("prepare_checkout succeeds", ok)
    check(
        ".lake linked",
        (co / ".lake").is_symlink() and os.path.realpath(co / ".lake") == os.path.realpath(root / "w6" / "dot-lake"),
    )
    check("the clean removed other leftovers", not (co / "stray.txt").exists())

    os.environ.pop(ENV, None)
    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
