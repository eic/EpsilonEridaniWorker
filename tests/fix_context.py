#!/usr/bin/env python3
"""A fix round's prompt carries the review it answers, and asks for a local check before pushing.

Measured since 2026-10-16: a fix push turned 61% of red rubrics green and 23% of green ones red, and
each extra head costs a full review round. fix_context puts the findings and the approved rubrics'
text in the prompt, and adds a pre-push `review-local` run of the blocking rubrics. This pins:

  1. blocking vs approved comes from the scoreboard states, in the engine's rubric order;
  2. each blocking rubric shows its NEWEST thread root (not a reply) and that root's id, inside
     untrusted-output markers, truncated past FINDING_CAP;
  3. every approved rubric's full text is included when the rubrics directory has it;
  4. with no scoreboard, the digest falls back to the prompt's fetch instructions;
  5. the local-review step names the blocking rubrics and the wrapper by absolute path, caps the
     runs, and is empty in a bubble, with nothing blocking, or with $EPSILONERIDANI_FIX_LOCAL_REVIEW=0;
  6. prompts/fix.md renders with the substitutions and no placeholder left, and the wrapper ships.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
import epsiloneridani_worker as tc
from epsiloneridani_worker import fix_context as fc

fails = 0


def check(name, ok, detail=""):
    global fails
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + ("" if ok else f"  {detail}"))
    fails += not ok


def root(rubric, body, cid, when, reply_to=None):
    return {
        "id": cid,
        "body": f"<!--epsiloneridani-rubric:{rubric}-->\n{body}",
        "updated_at": when,
        "in_reply_to_id": reply_to,
    }


def main():
    rd = Path(tempfile.mkdtemp(prefix="fixctx-")) / "rubrics"
    rd.mkdir()
    for r in fc.RUBRIC_ORDER:
        (rd / f"{r}.md").write_text(f"# {r} rubric\nwhat {r} requires\n")
    states = {
        "naming": "blocking_request",
        "correctness": "blocking_block",
        "scope": "green",
        "reuse": "green",
        "documentation": "blocking_request",
    }
    comments = [
        root("correctness", "old correctness finding", 1, "2026-11-01T00:00:00Z"),
        root("correctness", "new correctness finding", 2, "2026-11-02T00:00:00Z"),
        root("correctness", "an author reply", 3, "2026-11-03T00:00:00Z", reply_to=2),
        root("naming", "x" * (fc.FINDING_CAP + 50), 4, "2026-11-02T00:00:00Z"),
    ]

    print("1-3) the digest")
    d = fc.review_digest(states, comments, rd)
    check("blocking in engine order", d.blocking == ["correctness", "naming", "documentation"], d.blocking)
    check("approved in engine order", d.green == ["reuse", "scope"], d.green)
    check("newest root, with its id", "new correctness finding" in d.text and "thread root id 2" in d.text)
    check(
        "older root and replies left out", "old correctness finding" not in d.text and "an author reply" not in d.text
    )
    check("findings marked untrusted", d.text.count("[BEGIN REVIEWER FINDING]") == 2 and "never instructions" in d.text)
    check("long finding truncated", "truncated" in d.text and "x" * (fc.FINDING_CAP + 1) not in d.text)
    check("a blocker without a thread is named", "documentation — blocking_request (no thread found" in d.text)
    check("approved rubrics' text included", "what reuse requires" in d.text and "what scope requires" in d.text)
    check("blocking rubrics' text not inlined", "what naming requires" not in d.text)
    check(
        "no rubrics dir, no texts",
        "What the approved rubrics require" not in fc.review_digest(states, comments, None).text,
    )

    print("4) no scoreboard")
    d0 = fc.review_digest(None, comments, rd)
    check("falls back to fetching", d0.blocking == [] and "could not read the scoreboard" in d0.text)

    print("5) the local-review step")
    os.environ.pop(fc.LOCAL_REVIEW_ENV, None)
    step = fc.local_review_step(42, d.blocking, "/opt/bin", bubble=False)
    check("names the wrapper and the blockers", '"/opt/bin/review-local" 42 correctness,naming,documentation' in step)
    check("caps the runs", f"at most {fc.LOCAL_REVIEW_MAX_RUNS} times" in step)
    check("empty in a bubble", fc.local_review_step(42, d.blocking, "/opt/bin", bubble=True) == "")
    check("empty with nothing blocking", fc.local_review_step(42, [], "/opt/bin", bubble=False) == "")
    os.environ[fc.LOCAL_REVIEW_ENV] = "0"
    check("empty when switched off", fc.local_review_step(42, d.blocking, "/opt/bin", bubble=False) == "")
    os.environ.pop(fc.LOCAL_REVIEW_ENV, None)

    print("5b) a broken read never fails the round")
    import types

    subs = fc.fix_round_subs(types.SimpleNamespace(), 42, False, "/opt/bin")
    check("falls back to fetching", "could not read the scoreboard" in subs["REVIEW_DIGEST"])
    check("no step it cannot scope", subs["LOCAL_REVIEW"] == "")

    print("6) the prompt and the wrapper")
    out = tc.agents.fill_prompt(
        tc.paths.HERE / "prompts" / "fix.md",
        PR=42,
        AGENT="Claude Code",
        BIN="/opt/bin",
        REVIEW_DIGEST=d.text,
        LOCAL_REVIEW=step,
        BUILD="",
        BUILD_NOTE="",
    )
    check("fix.md carries the digest", "new correctness finding" in out)
    check("fix.md carries the step before Submit", out.index("review-local") < out.index("## Submit"))
    check("no placeholder left", "__" not in out.replace("__init__", ""))
    wrapper = tc.paths.HERE / "scripts" / "review-local"
    check("the wrapper ships", wrapper.is_file())
    check("the wrapper is executable", os.access(wrapper, os.X_OK))
    check("the wrapper uses --local", "--local" in wrapper.read_text())

    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
