"""epsiloneridani_worker.local_endpoint — a self-hosted, OpenAI-compatible model for `--agent local`.

The endpoint is described by a small env file that the serving side rewrites whenever it moves, e.g. a
Slurm job running vLLM behind a LiteLLM proxy writes, on every start:

    export OPENAI_BASE_URL="http://10.0.33.174:4000/v1"
    export OPENAI_API_KEY="sk-..."
    export OPENAI_MODEL="my-local-model"

The address changes with the node the job lands on, and the alias in OPENAI_MODEL can stand for
different weights from one job to the next (a coder model today, a prover tomorrow). So nothing here is
read once and remembered: every round re-reads the file, probes the endpoint, and asks the proxy which
model actually sits behind the alias, so the log says what wrote the code.

There is no subscription to pace against. Availability is reachability: a job that has ended removes
its env file, and one that is still queued has not written it yet; either way, and when the address
in the file does not answer, the loop waits for the next job instead of launching a round that would
fail at its first request. Only a missing setting or a malformed file is a configuration error.

The file is PARSED, never sourced: only `KEY=value` lines (an optional `export`, optional matching
quotes) are read, so a stray command in it is not executed by the worker. The API key never leaves
the process environment — the agent's provider config names the variable that holds it, not its value.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .config import Die

ENV_FILE_VAR = "EPSILONERIDANI_LOCAL_ENDPOINT_FILE"
# The variable the agent's provider config reads the key from. Its own name, not OPENAI_API_KEY, so
# the key cannot leak into a tool that happens to honour the OpenAI default.
API_KEY_ENV = "EPSILONERIDANI_LOCAL_API_KEY"
PI_PROVIDER = "local"
DEFAULT_CONTEXT_WINDOW = 131072
DEFAULT_MAX_TOKENS = 16384
PROBE_TIMEOUT_S = 10

_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*?)\s*$")


class EndpointDown(Exception):
    """The endpoint file is absent: no serving job is running right now. A wait, not an error."""


@dataclass(frozen=True)
class Endpoint:
    base_url: str  # ends in /v1
    api_key: str
    model: str  # the alias the proxy serves
    source: Path


def endpoint_file() -> Path | None:
    raw = (os.environ.get(ENV_FILE_VAR) or "").strip()
    return Path(raw).expanduser() if raw else None


def parse_env_file(text: str) -> dict[str, str]:
    """`KEY=value` pairs from a shell-style env file, without running it. Matching single or double
    quotes around a value are removed; anything that is not a plain assignment is ignored."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def read_endpoint(path: Path | None = None) -> Endpoint:
    """The endpoint as the file describes it right now. Raises EndpointDown when the file does not
    exist (the serving job removes it when it ends), and Die when the setting is missing or the file
    lacks a base URL — a configuration error, which waiting does not fix."""
    path = path or endpoint_file()
    if path is None:
        raise Die(f"--agent local needs ${ENV_FILE_VAR}: the env file that describes the endpoint")
    try:
        values = parse_env_file(path.read_text())
    except FileNotFoundError as e:
        raise EndpointDown(f"no endpoint file at {path} (is the serving job running?)") from e
    except OSError as e:
        raise Die(f"cannot read the local endpoint file {path}: {e}") from e
    base = (values.get("OPENAI_BASE_URL") or values.get("OPENAI_API_BASE") or "").strip().rstrip("/")
    if not base:
        raise Die(f"{path} sets neither OPENAI_BASE_URL nor OPENAI_API_BASE")
    return Endpoint(
        base_url=base,
        api_key=(values.get("OPENAI_API_KEY") or "").strip(),
        model=(values.get("OPENAI_MODEL") or "").strip(),
        source=path,
    )


def _get_json(url: str, api_key: str, timeout: float) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"} if api_key else {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — operator-configured URL
        return json.loads(resp.read().decode("utf-8", "replace"))


def probe(ep: Endpoint, model: str | None = None, *, timeout: float = PROBE_TIMEOUT_S) -> tuple[bool, str]:
    """(available, detail). Available when `/models` answers and lists `model` (default: the file's
    alias). `detail` says why not, or names what is being served."""
    want = model or ep.model
    try:
        listing = _get_json(f"{ep.base_url}/models", ep.api_key, timeout)
    except urllib.error.HTTPError as e:
        return False, f"local endpoint {ep.base_url} answered HTTP {e.code}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        reason = getattr(e, "reason", e)
        return False, f"local endpoint {ep.base_url} unreachable ({reason})"
    ids = [str(m.get("id")) for m in listing.get("data") or [] if isinstance(m, dict) and m.get("id")]
    if want and want not in ids:
        return False, f"local endpoint {ep.base_url} does not serve {want!r} (serves {', '.join(ids) or 'nothing'})"
    backing = backing_model(ep, want, timeout=timeout)
    return True, f"{want} → {backing}" if backing else (want or "")


def backing_model(ep: Endpoint, alias: str, *, timeout: float = PROBE_TIMEOUT_S) -> str | None:
    """The weights behind `alias`, from LiteLLM's /model/info (e.g. `Qwen/Qwen3-Coder-Next`), or None
    when the server is not LiteLLM or does not say. Best effort: never raises."""
    root = ep.base_url[: -len("/v1")] if ep.base_url.endswith("/v1") else ep.base_url
    for url in (f"{ep.base_url}/model/info", f"{root}/model/info"):
        try:
            info = _get_json(url, ep.api_key, timeout)
        except (urllib.error.URLError, OSError, ValueError):
            continue
        for entry in info.get("data") or []:
            if isinstance(entry, dict) and entry.get("model_name") == alias:
                served = str((entry.get("litellm_params") or {}).get("model") or "")
                return served.split("/", 1)[1] if served.startswith("openai/") else (served or None)
    return None


def pi_models_json(ep: Endpoint, model: str) -> dict:
    """pi's custom-provider config for this endpoint. `apiKey` names API_KEY_ENV (pi resolves an
    env-var name), so the file holds no secret. vLLM and LiteLLM reject the `developer` role and
    `reasoning_effort`, hence the compat switches."""
    return {
        "providers": {
            PI_PROVIDER: {
                "baseUrl": ep.base_url,
                "api": "openai-completions",
                "apiKey": API_KEY_ENV,
                "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": False},
                "models": [
                    {
                        "id": model,
                        "name": f"{model} (local)",
                        "contextWindow": int(os.environ.get("EPSILONERIDANI_LOCAL_CONTEXT", DEFAULT_CONTEXT_WINDOW)),
                        "maxTokens": int(os.environ.get("EPSILONERIDANI_LOCAL_MAX_TOKENS", DEFAULT_MAX_TOKENS)),
                        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                    }
                ],
            }
        }
    }


def write_pi_agent_dir(ep: Endpoint, model: str, agent_dir: Path) -> Path:
    """Write pi's config dir for one round (atomically, so a concurrent round never reads half a
    file) and return it. A private dir, not ~/.pi/agent: the operator's own pi settings, extensions,
    skills and sessions stay out of an unattended round."""
    agent_dir.mkdir(parents=True, exist_ok=True)
    target = agent_dir / "models.json"
    tmp = agent_dir / f".models.json.{os.getpid()}"
    tmp.write_text(json.dumps(pi_models_json(ep, model), indent=2) + "\n")
    os.replace(tmp, target)
    return agent_dir
