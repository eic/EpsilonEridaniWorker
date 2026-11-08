#!/usr/bin/env python3
"""$EPSILONERIDANI_BUILD_PREFIX puts a command (typically `srun ...`) in front of every compile-heavy
command the shipped prompts tell the agent to run.

On a cluster login node the worker's own limits (here a 36 GiB address-space cap) make Lean's thread
creation fail on a full build; a scheduler prefix runs those builds on a compute node instead. This
harness pins what makes that safe to switch on:

  1. Unset (the default), every shipped prompt renders byte-identically to its template with the two
     new placeholders removed: nothing changes for a worker that does not opt in.
  2. Set, every `lake build` / `lake env lean` / `lake exe axioms` / `lake exe module-system` line of
     every shipped prompt carries the prefix, and no cache download, git or gh line does (compute
     nodes often have no network). The prompt also says so in prose, for commands the agent writes.
  3. The prefix is re-quoted as shell words, so quoting survives; one that does not parse fails the
     round with a message naming the variable, not mid-build.
  4. A Bubble round never gets the prefix (it builds inside its sandbox).
  5. Every shipped call site passes the substitutions (fill_prompt would raise otherwise).

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
import epsiloneridani_worker as tc

agents = tc.agents
PROMPTS = tc.paths.HERE / "prompts"
ENV = agents.BUILD_PREFIX_ENV
HEAVY = re.compile(r"lake (?:build|env lean|exe axioms|exe module-system)\b")
NEVER = re.compile(r"lake (?:exe )?cache get|\bgit |\bgh ")
SUBS = dict(PR=7, AGENT="Claude Code", BIN="/bin", ONLY="A", SKIP="none", CLAIMED="none", FORK="f",
            WORKERID="w", ROADMAP_DIR="/r", REVIEW_DIR="/v", RUBRICS="/u", SOURCE_GUIDANCE="")

fails = 0


def check(name, ok, detail=""):
    global fails
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + ("" if ok else f"  {detail}"))
    fails += not ok


def render(path, prefix, bubble=False):
    if prefix is None:
        os.environ.pop(ENV, None)
    else:
        os.environ[ENV] = prefix
    return agents.fill_prompt(path, **SUBS, **agents.build_prompt_subs(bubble))


def main():
    prompts = sorted(PROMPTS.glob("*.md"))
    check("there are shipped prompts", len(prompts) >= 6)

    print("1) unset changes nothing")
    for p in prompts:
        tmpl = p.read_text().replace("__BUILD__", "").replace("__BUILD_NOTE__", "")
        want = re.sub(r"__([A-Z][A-Z_]*)__", lambda m: str(SUBS.get(m.group(1), m.group(0))), tmpl)
        check(f"{p.name} renders as before", render(p, None) == want)
        check(f"{p.name} blank prefix is unset", render(p, "   ") == want)

    print("2) set: every compile line and only compile lines")
    prefix = "srun --account=def-x --mem=64G -c 32"
    for p in prompts:
        out = render(p, prefix)
        lines = [ln.strip() for ln in out.splitlines()]
        heavy = [ln for ln in lines if HEAVY.match(ln.removeprefix(prefix + " "))
                 and not ln.startswith(("**", "-", "If", "Iterate"))]
        check(f"{p.name} has compile lines", bool(heavy))
        check(f"{p.name} every compile line is prefixed", all(ln.startswith(prefix + " lake ") for ln in heavy),
              [ln for ln in heavy if not ln.startswith(prefix)])
        check(f"{p.name} no cache/git/gh line is prefixed",
              not any(ln.startswith(prefix) and NEVER.search(ln) for ln in lines))
        check(f"{p.name} explains the rule", f"**Builds run through `{prefix}`.**" in out)

    print("3) quoting")
    out = render(PROMPTS / "fix.md", '  srun --job-name "ee build"  -c 8 ')
    check("re-quoted as shell words", "srun --job-name 'ee build' -c 8 lake build" in out)
    try:
        render(PROMPTS / "fix.md", 'srun --job-name "unterminated')
    except tc.config.Die as e:
        check("an unbalanced quote fails with the variable named", ENV in str(e), str(e))
    else:
        check("an unbalanced quote fails", False, "no Die raised")

    print("4) bubble ignores it")
    check("bubble round renders unprefixed", prefix not in render(PROMPTS / "fix.md", prefix, bubble=True))

    print("5) every shipped call site passes the substitutions")
    src = (REPO / "epsiloneridani_worker" / "work_units.py").read_text()
    shipped = re.findall(r'fill_prompt\(\s*HERE / "prompts"', src)
    check("three shipped call sites", len(shipped) == 3, len(shipped))
    check("each passes build_prompt_subs", src.count("**build_prompt_subs(") == len(shipped))

    os.environ.pop(ENV, None)
    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
