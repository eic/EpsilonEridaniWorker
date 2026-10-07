#!/usr/bin/env python3
"""`--agent local`: pi driving a self-hosted, OpenAI-compatible endpoint described by an env file.

Properties under test (no network; the endpoint is stubbed):

  - the endpoint file is PARSED, not sourced: `export`, quotes and comments are handled, and a line
    that is not a plain assignment is ignored rather than run;
  - a missing setting or base URL is a configuration error (Die); a missing FILE is a wait, because
    the serving job removes it when it ends;
  - availability is reachability: an unreachable endpoint, an HTTP error, or an endpoint not serving
    the alias reads unavailable with a reason; a live one names the weights behind the alias;
  - pi's provider config names the key's env var and never contains the key;
  - the host argv launches pi (via the venv's node when pi sits beside one) in --print mode, with a
    private PI_CODING_AGENT_DIR, the key in its own variable, and no OPENAI/ANTHROPIC keys;
  - a preflight (empty prompt) neither reads the endpoint file nor writes the config;
  - the authoring model defaults to the file's alias, and effort is refused;
  - commits and PRs credit that model by name (`Co-Authored-By: <model>`), not a generic "Local model";
  - Bubble is refused, and the CLI refuses a local worker whose stages include review;
  - the loop's availability gate re-reads the file every call, so a moved job is picked up;
  - a server without tool calling is unavailable (a wait), and if one slips past the gate its 400 is
    classified as infrastructure, so it is not charged to the PR;
  - pi is told the server's real context window (read from vLLM's own refusal of an impossible
    max_tokens), falling back small when the server does not say.
"""

import json
import os
import subprocess
import sys
import tempfile
import urllib.error
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import epsiloneridani_worker as tc
from epsiloneridani_worker import agents, local_endpoint, loop
from epsiloneridani_worker.config import Die

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")
    fails += not ok


def raises_die(fn) -> str | None:
    try:
        fn()
    except Die as e:
        return str(e)
    return None


tmp = Path(tempfile.mkdtemp(prefix="local-agent-"))
env_file = tmp / "endpoint.env"


def write_env(base="http://10.0.33.174:4000/v1", key="sk-secret-test", model="my-local-model"):
    env_file.write_text(
        "# written by the serving job\n"
        f'export OPENAI_API_BASE="{base}"\n'
        f'export OPENAI_BASE_URL="{base}"\n'
        f"export OPENAI_API_KEY={key}\n"
        f"export OPENAI_MODEL='{model}'\n"
        "echo this-line-must-not-run\n"
    )


os.environ[local_endpoint.ENV_FILE_VAR] = str(env_file)
os.environ["EPSILONERIDANI_DATA_HOME"] = str(tmp / "data")
for k in ("EPSILONERIDANI_AUTHORING_LOCAL_MODEL", "EPSILONERIDANI_AUTHORING_LOCAL_EFFORT", "EPSILONERIDANI_PI"):
    os.environ.pop(k, None)

# A fake vLLM-behind-LiteLLM for every POST the module makes, so no test can reach a real server. The
# texts are the ones a live LiteLLM → vLLM endpoint returned (only the numbers vary).
SERVER = {"tools": True, "max_model_len": 131072, "post_calls": []}
TOOLS_OFF = (
    '{"error":{"message":"litellm.BadRequestError: OpenAIException - \\"auto\\" tool choice requires '
    '--enable-auto-tool-choice and --tool-call-parser to be set. Received Model Group=my-local-model","code":"400"}}'
)


def fake_post(url, api_key, body, timeout):
    SERVER["post_calls"].append(body)
    if body.get("tools"):
        return (200, '{"choices":[]}') if SERVER["tools"] else (400, TOOLS_OFF)
    if body.get("max_tokens", 0) > 10**6:
        n = SERVER["max_model_len"]
        if n is None:
            return 400, '{"error":{"message":"bad request"}}'
        return 400, (
            f'{{"error":{{"message":"litellm.BadRequestError: OpenAIException - max_tokens={body["max_tokens"]} '
            f'cannot be greater than max_model_len=max_total_tokens={n}. Please request fewer output tokens."}}}}'
        )
    return 200, "{}"


local_endpoint._post_json = fake_post

# --- 1. parsing -----------------------------------------------------------------------------------
write_env()
vals = local_endpoint.parse_env_file(env_file.read_text())
check("export + double quotes", vals.get("OPENAI_BASE_URL"), "http://10.0.33.174:4000/v1")
check("unquoted value", vals.get("OPENAI_API_KEY"), "sk-secret-test")
check("single quotes", vals.get("OPENAI_MODEL"), "my-local-model")
check(
    "a command line is ignored, not run",
    sorted(vals),
    ["OPENAI_API_BASE", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"],
)
ep = local_endpoint.read_endpoint()
check("endpoint base", ep.base_url, "http://10.0.33.174:4000/v1")
check("endpoint model", ep.model, "my-local-model")

# --- 2. configuration errors are Die --------------------------------------------------------------
saved = os.environ.pop(local_endpoint.ENV_FILE_VAR)
check("unset file var is Die", bool(raises_die(local_endpoint.read_endpoint)), True)
os.environ[local_endpoint.ENV_FILE_VAR] = str(tmp / "missing.env")
try:
    local_endpoint.read_endpoint()
    down = None
except local_endpoint.EndpointDown as e:
    down = str(e)
check("missing file is EndpointDown (no serving job), not Die", bool(down) and "serving job" in down, True)
live, why = loop.local_agent_available()
check("gate: missing file is a wait", (live, "no endpoint file" in why), (False, True))
try:
    agents._local_agent_argv("prompt", "my-local-model", {})
    backed_off = False
except tc.config.NoProgress:
    backed_off = True
check("round: file gone since the gate backs off (NoProgress)", backed_off, True)
(tmp / "nobase.env").write_text("export OPENAI_MODEL=x\n")
os.environ[local_endpoint.ENV_FILE_VAR] = str(tmp / "nobase.env")
check("no base URL is Die", "OPENAI_BASE_URL" in (raises_die(local_endpoint.read_endpoint) or ""), True)
os.environ[local_endpoint.ENV_FILE_VAR] = saved


# --- 3. probe -------------------------------------------------------------------------------------
def served(models=("my-local-model",), backing="openai/Qwen/Qwen3-Coder-Next"):
    def fake(url, api_key, timeout):
        assert api_key == "sk-secret-test", api_key
        if url.endswith("/models"):
            return {"data": [{"id": m} for m in models]}
        if url.endswith("/model/info"):
            return {"data": [{"model_name": "my-local-model", "litellm_params": {"model": backing}}]}
        raise AssertionError(url)

    return fake


with patch.object(local_endpoint, "_get_json", side_effect=served()):
    check(
        "live endpoint names the backing weights",
        local_endpoint.probe(ep),
        (True, "my-local-model → Qwen/Qwen3-Coder-Next"),
    )
with patch.object(local_endpoint, "_get_json", side_effect=served(models=("other",))):
    live, why = local_endpoint.probe(ep)
    check("alias not served: unavailable", live, False)
    check("alias not served: says what is", "serves other" in why, True)
with patch.object(local_endpoint, "_get_json", side_effect=urllib.error.URLError("timed out")):
    live, why = local_endpoint.probe(ep)
    check("unreachable: unavailable", live, False)
    check("unreachable: reason", "unreachable (timed out)" in why, True)
with patch.object(local_endpoint, "_get_json", side_effect=urllib.error.HTTPError("u", 401, "no", {}, None)):
    check("HTTP error: unavailable", local_endpoint.probe(ep)[0], False)

# --- 4. pi config holds no secret -----------------------------------------------------------------
cfg = local_endpoint.pi_models_json(ep, "my-local-model")
prov = cfg["providers"]["local"]
check("pi config names the key variable", prov["apiKey"], local_endpoint.API_KEY_ENV)
check("pi config never contains the key", "sk-secret-test" in json.dumps(cfg), False)
check("pi config uses chat completions", prov["api"], "openai-completions")
check("pi config base", prov["baseUrl"], ep.base_url)

# --- 5. host argv ---------------------------------------------------------------------------------
fakebin = tmp / "venv-bin"
fakebin.mkdir()
(fakebin / "pi").write_text("#!/usr/bin/env node\n")
(fakebin / "node").write_text("")
os.environ["EPSILONERIDANI_PI"] = str(fakebin / "pi")
os.environ["OPENAI_API_KEY"] = "should-not-leak"
os.environ["ANTHROPIC_API_KEY"] = "should-not-leak"
argv, env = agents.host_agent_argv("do the thing", "local")
check("launches the venv node with pi", argv[:2], [str(fakebin / "node"), str(fakebin / "pi")])
check("pi flags", argv[2:-1], ["--provider", "local", "--model", "my-local-model", "--print", "--no-session"])
check("prompt is last", argv[-1], "do the thing")
check("key in its own variable", env.get(local_endpoint.API_KEY_ENV), "sk-secret-test")
check("no OPENAI_API_KEY", "OPENAI_API_KEY" in env, False)
check("no ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY" in env, False)
check("offline startup", env.get("PI_OFFLINE"), "1")
agent_dir = Path(env["PI_CODING_AGENT_DIR"])
check("private agent dir under the data home", str(agent_dir).startswith(str(tmp / "data")), True)
written = json.loads((agent_dir / "models.json").read_text())
check("models.json written for this round", written["providers"]["local"]["baseUrl"], ep.base_url)
check("PATH not given the venv bin", str(fakebin) in env.get("PATH", "").split(os.pathsep), False)
os.environ.pop("OPENAI_API_KEY")
os.environ.pop("ANTHROPIC_API_KEY")

# --- 6. preflight reads nothing -------------------------------------------------------------------
(agent_dir / "models.json").unlink()
saved = os.environ.pop(local_endpoint.ENV_FILE_VAR)
pf_argv, _ = agents._local_agent_argv("", "my-local-model", {})
check("preflight argv still names pi", pf_argv[1], str(fakebin / "pi"))
check("preflight wrote no config", (agent_dir / "models.json").exists(), False)
check("preflight gate is pi itself", tc.work_units._host_agent_binary("fix-ci", "local"), str(fakebin / "pi"))
os.environ[local_endpoint.ENV_FILE_VAR] = saved

# --- 7. authoring profile -------------------------------------------------------------------------
prof = agents.resolve_authoring_profile("local")
check("model defaults to the file's alias", prof.model, "my-local-model")
check("--author-model overrides", agents.resolve_authoring_profile("local", cli_model="leanstral").model, "leanstral")
check("effort refused", bool(raises_die(lambda: agents.resolve_authoring_profile("local", cli_effort="high"))), True)
# An override must not need the file's model: a file that names none still works with --author-model
# or the env pin, and the file is not consulted for the model at all.
(tmp / "nomodel.env").write_text('export OPENAI_BASE_URL="http://10.0.33.174:4000/v1"\n')
saved = os.environ[local_endpoint.ENV_FILE_VAR]
os.environ[local_endpoint.ENV_FILE_VAR] = str(tmp / "nomodel.env")
check(
    "no OPENAI_MODEL and no override is Die",
    "OPENAI_MODEL" in (raises_die(lambda: agents.resolve_authoring_profile("local")) or ""),
    True,
)
check(
    "--author-model works with a model-less file",
    agents.resolve_authoring_profile("local", cli_model="leanstral").model,
    "leanstral",
)
os.environ["EPSILONERIDANI_AUTHORING_LOCAL_MODEL"] = "leanstral-env"
check("env pin works with a model-less file", agents.resolve_authoring_profile("local").model, "leanstral-env")
os.environ.pop("EPSILONERIDANI_AUTHORING_LOCAL_MODEL")
os.environ[local_endpoint.ENV_FILE_VAR] = str(tmp / "absent.env")
check(
    "--author-model needs no file at all",
    agents.resolve_authoring_profile("local", cli_model="leanstral").model,
    "leanstral",
)
os.environ[local_endpoint.ENV_FILE_VAR] = saved
check(
    "file default records its source",
    agents.resolve_authoring_profile("local").model_source,
    "endpoint file OPENAI_MODEL",
)

# The agent name the prompts write into `Co-Authored-By:` and the PR footer is the model, not the
# generic "Local model", falling back to it only when no model can be resolved.
local_opts = dict(only=["fix"], agent="local", work_model="local", sandbox_host=True, dry_run=False)
check("agent name is the file's model", tc.RoundOpts(**local_opts).agent_name, "my-local-model")
check(
    "agent name follows a pinned profile",
    tc.RoundOpts(
        **local_opts, authoring_profile=agents.resolve_authoring_profile("local", cli_model="leanstral")
    ).agent_name,
    "leanstral",
)
os.environ[local_endpoint.ENV_FILE_VAR] = str(tmp / "nomodel.env")
check("agent name falls back without a model", tc.RoundOpts(**local_opts).agent_name, "Local model")
os.environ[local_endpoint.ENV_FILE_VAR] = str(tmp / "absent.env")
check("agent name falls back without a file", tc.RoundOpts(**local_opts).agent_name, "Local model")
os.environ[local_endpoint.ENV_FILE_VAR] = saved
check(
    "other agents keep their names",
    tc.RoundOpts(only=["fix"], agent="claude", work_model="claude", sandbox_host=True, dry_run=False).agent_name,
    "Claude Code",
)

# --- 8. bubble refused ----------------------------------------------------------------------------
check("bubble refused", "host only" in (raises_die(lambda: agents.agent_inner_cmd(prof)) or ""), True)

# --- 9. the loop gate re-reads the file each call -------------------------------------------------
seen = []


def recorder(url, api_key, timeout):
    seen.append(url)
    return served()(url, api_key, timeout)


with patch.object(local_endpoint, "_get_json", side_effect=recorder):
    check("gate: live", loop.local_agent_available()[0], True)
    write_env(base="http://10.0.99.1:4000/v1")  # the job moved
    loop.local_agent_available()
check("gate: picked up the moved job", any(u.startswith("http://10.0.99.1:4000/v1/") for u in seen), True)

# The gate probes for the model the round will launch: --author-model, not the file's alias.
with patch.object(local_endpoint, "_get_json", side_effect=served(models=("leanstral",))):
    check("gate: probes the --author-model pin", loop.local_agent_available("leanstral")[0], True)
    check("gate: without the pin, the file's alias is missing", loop.local_agent_available()[0], False)
write_env()

# --- 10. tool calling and context window ----------------------------------------------------------
SERVER["tools"] = False
with patch.object(local_endpoint, "_get_json", side_effect=served()):
    live, why = local_endpoint.probe(ep)
check("tools disabled: unavailable even though /models answers", live, False)
check(
    "tools disabled: says how to fix the server", "--enable-auto-tool-choice" in why and "Qwen3-Coder-Next" in why, True
)
SERVER["tools"] = True
check("tool probe asks for one token", [b.get("max_tokens") for b in SERVER["post_calls"] if b.get("tools")][-1], 1)

# 24576, not 32768: the fallback is 32768, so only a distinct value proves the refusal was parsed.
SERVER["max_model_len"] = 24576
check("context: read from vLLM's refusal", local_endpoint.context_window(ep, "my-local-model"), 24576)
SERVER["max_model_len"] = None
check(
    "context: unreadable falls back small",
    local_endpoint.context_window(ep, "my-local-model"),
    local_endpoint.FALLBACK_CONTEXT_WINDOW,
)
os.environ["EPSILONERIDANI_LOCAL_CONTEXT"] = "65536"
check("context: env pin wins", local_endpoint.context_window(ep, "my-local-model"), 65536)
os.environ.pop("EPSILONERIDANI_LOCAL_CONTEXT")
SERVER["max_model_len"] = 24576
m = local_endpoint.pi_models_json(ep, "my-local-model", 24576)["providers"]["local"]["models"][0]
check("pi gets the real window", m["contextWindow"], 24576)
check("output limit is a quarter of a small window", m["maxTokens"], 6144)
m = local_endpoint.pi_models_json(ep, "my-local-model", 131072)["providers"]["local"]["models"][0]
check("output limit capped for a large window", m["maxTokens"], local_endpoint.DEFAULT_MAX_TOKENS)
argv, env = agents.host_agent_argv("do the thing", "local")
written = json.loads((Path(env["PI_CODING_AGENT_DIR"]) / "models.json").read_text())
check(
    "the round's models.json carries the discovered window",
    written["providers"]["local"]["models"][0]["contextWindow"],
    24576,
)
SERVER["max_model_len"] = 131072

# --- 11. a tools-disabled 400 is the server's fault, not the PR's ---------------------------------
pi_log = (
    '400 litellm.BadRequestError: OpenAIException - "auto" tool choice requires --enable-auto-tool-choice '
    "and --tool-call-parser to be set. Received Model Group=my-local-model\n"
    "Available Model Group Fallbacks=None\n"
)
check(
    "classified as infrastructure (refunded)",
    agents.classify_agent_failure(pi_log),
    "the model server has tool calling disabled",
)
for body in ("   ", '{"error":{"message":"   "}}', ""):
    check(f"blank error body {body!r} gives an empty message", local_endpoint._error_message(body), "")
check("no error.message falls back to the body", local_endpoint._error_message('{"error":{}}'), '{"error":{}}')
check(
    "error message is the first line of error.message",
    local_endpoint._error_message('{"error":{"message":"bad thing\\nmore"}}'),
    "bad thing",
)
check("an ordinary 400 is still the task's", agents.classify_agent_failure("400 Bad Request: prompt too long\n"), None)

# --- 12. CLI refuses review for a local worker ----------------------------------------------------
for extra in ([], ["--only", "review"], ["--only", "fix-ci,review"]):
    r = subprocess.run(
        [sys.executable, "-m", "epsiloneridani_worker", "work", "--agent", "local", "--dry-run", *extra],
        cwd=REPO,
        capture_output=True,
        text=True,
        env={**os.environ, "EPSILONERIDANI_STATE": str(tmp / "state")},
        timeout=120,
    )
    out = r.stdout + r.stderr
    check(f"CLI refuses {extra or 'the default cascade'}", ("authors only" in out, r.returncode != 0), (True, True))

print()
if fails:
    print(f"FAIL: {fails} mismatch(es)")
    sys.exit(1)
print("all local agent checks passed")
