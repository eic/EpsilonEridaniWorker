#!/usr/bin/env python3
"""Every Claude agent run appends what it spent to its credential source's spend journal.

The usage endpoint answers most polls with 429, so the workers spend long stretches without a fresh
reading. Estimating usage between readings needs to know what each run spent, which Claude Code
reports in its `result` (a cost estimate and tokens per model) and its rate_limit_event headers. Until
now the transcript kept only the turn count and duration. Pinned:

  1. A run through run_agent_proc writes one JSON line to <credential source>/.epsiloneridani-quota/
     spend.jsonl: worker, label, rc, start and end, spend (cost, tokens, per model) and the last
     rate-limit headers per window, and logs the spend in the main log.
  2. Under an isolated home the journal follows the .epsiloneridani-creds-source marker to the real
     credential dir, which is where the shared usage reading lives too.
  3. A failed run is journaled as well, with its rc; one that reported no spend is journaled with spend
     null rather than skipped; one interrupted mid-run (the worker stopped) keeps what it had reported,
     with rc null. Non-Claude providers write nothing.
  4. A journal that cannot be written never fails the run.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import epsiloneridani_worker as tc  # noqa: E402

agents = tc.agents
quota = tc.quota
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}" + ("" if cond else f"  {detail}"))


EVENTS = [
    {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-opus-5-5"},
    {
        "type": "rate_limit_event",
        "session_id": "s1",
        "rate_limit_info": {
            "status": "allowed",
            "rateLimitType": "five_hour",
            "resetsAt": 1794691200,
            "utilization": 0.42,
        },
    },
    {"type": "assistant", "session_id": "s1", "message": {"content": [{"type": "text", "text": "done"}]}},
    {
        "type": "result",
        "subtype": "success",
        "session_id": "s1",
        "num_turns": 3,
        "duration_ms": 1000,
        "total_cost_usd": 2.5,
        "modelUsage": {
            "claude-opus-5-5": {
                "inputTokens": 30,
                "outputTokens": 12000,
                "cacheReadInputTokens": 500000,
                "cacheCreationInputTokens": 20000,
                "costUSD": 2.5,
            }
        },
    },
]


def fake_agent(tmp: Path, events, rc=0) -> list[str]:
    script = tmp / "fake-claude.py"
    lines = "".join(json.dumps(e) + "\n" for e in events)
    script.write_text(f"import sys\nsys.stdout.write({lines!r})\nsys.exit({rc})\n")
    return [sys.executable, str(script)]


def run(tmp: Path, events, *, rc=0, provider="claude"):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        got = agents.run_agent_proc(
            fake_agent(tmp, events, rc),
            env=dict(os.environ),
            logdir=tmp / "logs",
            label=f"agent-{provider}",
            provider=provider,
        )
    return got, out.getvalue()


def journal(claude: Path) -> list[dict]:
    path = claude / quota.CLAUDE_QUOTA_DIRNAME / quota.CLAUDE_SPEND_JOURNAL_FILE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def main():
    saved = {
        k: os.environ.get(k) for k in ("HOME", "CLAUDE_CONFIG_DIR", "EPSILONERIDANI_WORKER_ID", "EPSILONERIDANI_STREAM")
    }
    try:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            home = tmp / "home"
            real = tmp / "operator-claude"
            home.mkdir()
            real.mkdir()
            os.environ["HOME"] = str(home)
            os.environ["CLAUDE_CONFIG_DIR"] = str(real)
            os.environ["EPSILONERIDANI_WORKER_ID"] = "worker27"
            os.environ.pop("EPSILONERIDANI_STREAM", None)

            print("1) a run is journaled")
            rc, out = run(tmp, EVENTS)
            lines = journal(real)
            check("the run succeeded", rc == 0, out)
            check("one journal line", len(lines) == 1, lines)
            entry = lines[0] if lines else {}
            check(
                "names the worker, label and rc",
                (entry.get("worker"), entry.get("label"), entry.get("rc")) == ("worker27", "agent-claude", 0),
            )
            check(
                "has start and end times", entry.get("started", 0) > 0 and entry.get("ts", 0) >= entry.get("started", 0)
            )
            spend = entry.get("spend") or {}
            check("records cost", spend.get("cost_usd") == 2.5, spend)
            check(
                "records tokens",
                spend.get("tokens") == {"input": 30, "output": 12000, "cache_read": 500000, "cache_write": 20000},
                spend,
            )
            check("records tokens per model", spend.get("models", {}).get("claude-opus-5-5", {}).get("output") == 12000)
            limits = entry.get("rate_limits") or {}
            check("records the last rate-limit headers", limits.get("five_hour", {}).get("utilization") == 0.42, limits)
            check("logs the spend in the main log", "agent-claude: spent $2.50, 12,000 output" in out, out)

            print("2) an isolated home journals at the real credential source")
            iso = tmp / "iso-claude"
            iso.mkdir()
            (iso / ".epsiloneridani-creds-source").write_text(str(real) + "\n")
            os.environ["CLAUDE_CONFIG_DIR"] = str(iso)
            run(tmp, EVENTS)
            check("appended to the real dir's journal", len(journal(real)) == 2)
            check("nothing in the per-worker copy", journal(iso) == [])
            check(
                "the same dir the shared usage reading uses",
                quota.claude_quota_dir(home) == real / quota.CLAUDE_QUOTA_DIRNAME,
            )

            print("3) failures and other providers")
            rc, _ = run(
                tmp,
                EVENTS[:1]
                + [{"type": "result", "subtype": "error_during_execution", "session_id": "s1", "is_error": True}],
                rc=1,
            )
            last = journal(real)[-1]
            check("a failed run is journaled with its rc", rc == 1 and last["rc"] == 1)
            check("...and spend null when it reported none", last["spend"] is None)

            class Interrupted(tc.AgentTranscriptRenderer):
                def render_line(self, raw):
                    out = super().render_line(raw)
                    if '"result"' in raw:
                        raise KeyboardInterrupt
                    return out

            saved_renderer, agents.AgentTranscriptRenderer = agents.AgentTranscriptRenderer, Interrupted
            try:
                run(tmp, EVENTS)
                interrupted = False
            except KeyboardInterrupt:
                interrupted = True
            finally:
                agents.AgentTranscriptRenderer = saved_renderer
            last = journal(real)[-1]
            check("an interrupted run still raises", interrupted)
            check(
                "...and is journaled with rc null and what it had spent",
                last["rc"] is None and (last["spend"] or {}).get("cost_usd") == 2.5,
                last,
            )
            run(tmp, ["not json"], provider="codex")
            check("another provider writes nothing", len(journal(real)) == 4)

            print("4) an unwritable journal")
            blocker = tmp / "blocked"
            blocker.write_text("a file where the credential dir should be")
            os.environ["CLAUDE_CONFIG_DIR"] = str(blocker)
            rc, out = run(tmp, EVENTS)
            check("does not fail the run", rc == 0, out)
            check("the writer reports it", quota.record_claude_spend(home, {"x": 1}) is None)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
