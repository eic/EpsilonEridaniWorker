#!/usr/bin/env python3
"""claim.sh must refuse to take, renew, release or check a claim when EPSILONERIDANI_WORKER_ID is unset.

The owner id used to default to `hostname-$$`, a different owner for every claim.sh process, so a claim
taken by one call read as "held by another" on the next, and git-safe-push then failed closed with
"lease lost (another agent took over)". These tests run the real scripts against a local bare remote
(via url.insteadOf), so no network is touched.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLAIM_SH = REPO / "scripts" / "claim.sh"
GIT_SAFE_PUSH = REPO / "scripts" / "git-safe-push"
GH_SAFE_PR_CREATE = REPO / "scripts" / "gh-safe-pr-create"
CLAIMS = "test/claims"
KEY = "author/Area/target"
REF = f"refs/epsiloneridani-claims/{KEY}"


class Sandbox:
    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="claim-worker-id-"))
        self.remote = self.tmp / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        base = {k: v for k, v in os.environ.items() if k != "EPSILONERIDANI_WORKER_ID"}
        self.env = {
            **base,
            "HOME": str(self.tmp / "home"),
            "CLAIM_REPO": CLAIMS,
            "CLAIM_GITDIR_BASE": str(self.tmp / "claims"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.{self.remote.as_uri()}.insteadOf",
            "GIT_CONFIG_VALUE_0": f"https://github.com/{CLAIMS}",
        }
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run(self, argv, wid=None, **extra):
        env = {**self.env, **extra}
        if wid is not None:
            env["EPSILONERIDANI_WORKER_ID"] = wid
        return subprocess.run([str(a) for a in argv], env=env, capture_output=True, text=True, cwd=self.tmp)

    def claim(self, *args, wid=None):
        return self.run([CLAIM_SH, *args], wid=wid)

    def remote_oid(self):
        return subprocess.run(
            ["git", "-C", str(self.remote), "for-each-ref", "--format=%(objectname)", REF],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()


def check(name, fn):
    try:
        fn()
        print(f"[OK ] {name}")
        return 0
    except Exception as exc:
        print(f"[BAD] {name}: {exc}")
        return 1


def owner_commands_fail_without_worker_id():
    with Sandbox() as s:
        r = s.claim("acquire", KEY)
        assert r.returncode == 2, f"acquire rc={r.returncode}, want 2: {r.stderr}"
        assert "EPSILONERIDANI_WORKER_ID" in r.stderr, f"acquire did not name the variable: {r.stderr!r}"
        assert not s.remote_oid(), "acquire without a worker id created a claim ref"
        assert s.claim("acquire", KEY, wid="w1").returncode == 0
        before = s.remote_oid()
        for cmd in ("renew", "release", "holds"):
            r = s.claim(cmd, KEY)
            assert r.returncode == 2, f"{cmd} rc={r.returncode}, want 2: {r.stderr}"
            assert "EPSILONERIDANI_WORKER_ID" in r.stderr, f"{cmd} did not name the variable: {r.stderr!r}"
        assert s.remote_oid() == before, "a command without a worker id changed the claim ref"


def read_only_commands_need_no_worker_id():
    with Sandbox() as s:
        assert s.claim("acquire", KEY, wid="w1").returncode == 0
        r = s.claim("read", KEY)
        assert r.returncode == 0 and '"owner":"w1"' in r.stdout, f"read rc={r.returncode}: {r.stdout!r} {r.stderr!r}"
        r = s.claim("list")
        assert r.returncode == 0 and KEY in r.stdout, f"list rc={r.returncode}: {r.stdout!r} {r.stderr!r}"


def a_stable_worker_id_recognises_its_own_claim():
    with Sandbox() as s:
        assert s.claim("acquire", KEY, wid="w1").returncode == 0
        assert s.claim("holds", KEY, wid="w1").returncode == 0, "the owner does not hold its own claim"
        assert s.claim("holds", KEY, wid="w2").returncode == 1, "another worker holds w1's claim"
        assert s.claim("acquire", KEY, wid="w2").returncode == 1, "another worker took a live claim"


def git_safe_push_reports_a_missing_worker_id():
    with Sandbox() as s:
        assert s.claim("acquire", KEY, wid="w1").returncode == 0
        r = s.run([GIT_SAFE_PUSH, "some-branch"], EPSILONERIDANI_CLAIM_KEY=KEY, EPSILONERIDANI_CLAIM_SH=str(CLAIM_SH))
        assert r.returncode != 0, "git-safe-push went ahead without a worker id"
        assert "EPSILONERIDANI_WORKER_ID" in r.stderr, f"no mention of the variable: {r.stderr!r}"
        assert "another agent took over" not in r.stderr, f"misreported as a lost lease: {r.stderr!r}"


def gh_safe_pr_create_reports_a_missing_worker_id():
    with Sandbox() as s:
        assert s.claim("acquire", KEY, wid="w1").returncode == 0
        r = s.run(
            [GH_SAFE_PR_CREATE, "--title", "t"],
            EPSILONERIDANI_AUTHOR_CLAIM_KEY=KEY,
            EPSILONERIDANI_CLAIM_SH=str(CLAIM_SH),
        )
        assert r.returncode != 0, "gh-safe-pr-create went ahead without a worker id"
        assert "EPSILONERIDANI_WORKER_ID" in r.stderr, f"no mention of the variable: {r.stderr!r}"
        assert "another agent took the target" not in r.stderr, f"misreported as a lost lease: {r.stderr!r}"


if not shutil.which("jq"):
    print("SKIP: jq not installed (claim.sh needs it)")
    sys.exit(0)

fails = sum(
    check(name, case)
    for name, case in (
        ("acquire/renew/release/holds exit 2 without a worker id", owner_commands_fail_without_worker_id),
        ("read and list need no worker id", read_only_commands_need_no_worker_id),
        ("a stable worker id recognises its own claim", a_stable_worker_id_recognises_its_own_claim),
        ("git-safe-push reports a missing worker id, not a lost lease", git_safe_push_reports_a_missing_worker_id),
        ("gh-safe-pr-create reports a missing worker id, not a lost lease", gh_safe_pr_create_reports_a_missing_worker_id),
    )
)
print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
