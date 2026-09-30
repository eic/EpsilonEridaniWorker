#!/usr/bin/env python3
"""A lease object that can't be written must never turn a claim push into a DELETE of the claim.

`claim.sh` builds each lease as an orphan commit and pushes `<oid>:<ref>`. If `git commit-tree` fails
(e.g. "Disk quota exceeded" on the claim scratch repo), an unchecked empty oid makes the refspec
`:<ref>`, which deletes the claim and exits 0; the heartbeat then "renews" the worker's own claim away
and git-safe-push later fails closed with "lease lost". These tests run the real script against a local
bare remote (via url.insteadOf) with a `git` shim that fails `commit-tree`, so no network is touched.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLAIM_SH = REPO / "scripts" / "claim.sh"
CLAIMS = "test/claims"
KEY = "branch/66"
REF = f"refs/epsiloneridani-claims/{KEY}"

SHIM = """#!/usr/bin/env bash
for a in "$@"; do
    if [[ "$a" == commit-tree ]]; then echo "error: unable to create temporary file: Disk quota exceeded" >&2; exit 128; fi
done
exec "$REAL_GIT" "$@"
"""


class Sandbox:
    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="claim-write-failure-"))
        self.remote = self.tmp / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        shim_dir = self.tmp / "shim"
        shim_dir.mkdir()
        (shim_dir / "git").write_text(SHIM)
        (shim_dir / "git").chmod(0o755)
        self.shim_path = f"{shim_dir}{os.pathsep}{os.environ['PATH']}"
        self.env = {
            **os.environ,
            "HOME": str(self.tmp / "home"),
            "CLAIM_REPO": CLAIMS,
            "CLAIM_GITDIR_BASE": str(self.tmp / "claims"),
            "EPSILONERIDANI_WORKER_ID": "worker-test",
            "REAL_GIT": shutil.which("git"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.{self.remote.as_uri()}.insteadOf",
            "GIT_CONFIG_VALUE_0": f"https://github.com/{CLAIMS}",
        }
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def claim(self, *args, broken=False):
        env = {**self.env, "PATH": self.shim_path} if broken else self.env
        return subprocess.run([str(CLAIM_SH), *args], env=env, capture_output=True, text=True)

    def remote_oid(self):
        out = subprocess.run(
            ["git", "-C", str(self.remote), "for-each-ref", "--format=%(objectname)", REF],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return out


def check(name, fn):
    try:
        fn()
        print(f"[OK ] {name}")
        return 0
    except Exception as exc:
        print(f"[BAD] {name}: {exc}")
        return 1


def renew_write_failure_keeps_claim():
    with Sandbox() as s:
        r = s.claim("acquire", KEY)
        assert r.returncode == 0, r.stderr
        before = s.remote_oid()
        assert before, "acquire did not create the claim ref"
        r = s.claim("renew", KEY, broken=True)
        assert r.returncode == 2, f"renew rc={r.returncode}, want 2 (error): {r.stderr}"
        assert s.remote_oid() == before, "a failed renew changed or DELETED the claim ref"
        assert s.claim("holds", KEY).returncode == 0, "worker no longer holds its claim after a failed renew"


def acquire_write_failure_is_an_error():
    with Sandbox() as s:
        r = s.claim("acquire", KEY, broken=True)
        assert r.returncode == 2, f"acquire rc={r.returncode}, want 2 (error, not 'claimed elsewhere'): {r.stderr}"
        assert not s.remote_oid(), "a failed acquire created a claim ref"


def reacquire_write_failure_keeps_claim():
    with Sandbox() as s:
        assert s.claim("acquire", KEY).returncode == 0
        before = s.remote_oid()
        r = s.claim("acquire", KEY, broken=True)
        assert r.returncode == 2, f"re-acquire rc={r.returncode}, want 2: {r.stderr}"
        assert s.remote_oid() == before, "a failed re-acquire changed or DELETED the claim ref"


def healthy_renew_still_works():
    with Sandbox() as s:
        assert s.claim("acquire", KEY).returncode == 0
        before = s.remote_oid()
        r = s.claim("renew", KEY)
        assert r.returncode == 0, r.stderr
        after = s.remote_oid()
        assert after and after != before, "renew did not move the lease forward"


if not shutil.which("jq"):
    print("SKIP: jq not installed (claim.sh needs it)")
    sys.exit(0)

fails = sum(
    check(name, case)
    for name, case in (
        ("a renew that can't write its lease leaves the claim in place", renew_write_failure_keeps_claim),
        ("an acquire that can't write its lease is an error, not a dedup skip", acquire_write_failure_is_an_error),
        ("a re-acquire that can't write its lease leaves the claim in place", reacquire_write_failure_keeps_claim),
        ("a healthy renew still extends the lease", healthy_renew_still_works),
    )
)
print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
