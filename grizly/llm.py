"""One `claude -p` run per company with a strict JSON schema.

The schema has a fixed core (industry, description, brand, personalization with
a verbatim quote, contacts) plus the seller's own signals from the profile.
Claude Code runs LEAN: our instruction replaces its system prompt, no tools,
no MCP servers, no skills, no settings (so no hooks), no CLAUDE.md (empty
working directory), no saved session. Measured 2026-11-22 on a machine with
many MCP servers: the default run sent ~40 700 tokens of overhead ($0.16 per
company, over the $0.15 budget), the lean run ~1 200 tokens ($0.005).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from grizly.profile import Profile

MAX_TOTAL_CHARS = 40_000  # all pages together; keeps the cost per company flat

# Canonical tool names only — an unknown name could break the run.
NO_TOOLS = ",".join([
    "Bash", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit",
    "Glob", "Grep", "WebFetch", "WebSearch", "Task", "TodoWrite",
])
# Flags the lean run needs; `selftest` checks the installed CLI has them all.
LEAN_FLAGS = ("--system-prompt", "--tools", "--strict-mcp-config", "--disable-slash-commands",
              "--no-session-persistence", "--setting-sources", "--json-schema",
              "--max-budget-usd")

CORE_REQUIRED = ["industry", "short_description", "trade_name", "personalization",
                 "personalization_quote", "contacts"]
JSON_TYPES = {"boolean": "boolean", "integer": "integer", "number": "number", "string": "string"}


def build_schema(profile: Profile) -> dict[str, Any]:
    props: dict[str, Any] = {
        "industry": {"type": ["string", "null"],
                     "description": "2-4 words, human: 'building materials wholesale'."},
        "short_description": {"type": ["string", "null"],
                              "description": "One sentence: what the company does, for whom."},
        "trade_name": {"type": ["string", "null"],
                       "description": "Brand the company uses publicly, if it differs from "
                                      "the legal name."},
        "personalization": {"type": ["string", "null"],
                            "description": "One specific sentence for the first line of a "
                                           "message."},
        "personalization_quote": {"type": ["string", "null"],
                                  "description": "Verbatim excerpt of the page text the "
                                                 "personalization rests on."},
        "contacts": {
            "type": "array", "maxItems": 3,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["name", "role", "phone", "email"],
                "properties": {
                    "name": {"type": ["string", "null"]},
                    "role": {"type": ["string", "null"]},
                    "phone": {"type": ["string", "null"]},
                    "email": {"type": ["string", "null"]},
                },
            },
        },
    }
    for s in profile.signals:
        props[s.name] = {"type": [JSON_TYPES[s.type], "null"], "description": s.description}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": CORE_REQUIRED + [s.name for s in profile.signals],
        "properties": props,
    }


def build_instruction(profile: Profile, language_name: str) -> str:
    lines = [
        "You are a data analyst. From the website text of the company below, extract the "
        "fields of a sales lead card.",
        "",
        "Rules you must follow:",
        "1. Do not invent anything. If the text does not say it, return null. Null is a "
        "correct answer.",
        "2. `personalization` only when the text supports it. It must be a specific fact (new "
        "branch, new product, job ad, article, award), not generic praise such as \"modern "
        "company\" or \"family tradition\".",
        "3. `personalization_quote` is a VERBATIM excerpt of the page text the personalization "
        "is based on. It must be findable in the text. Do not change a single character.",
        "4. `contacts`: only work contacts the company itself published. Never guess e-mails "
        "from patterns like firstname.lastname@. If the site shows no phone, return null.",
        f"5. Write `industry`, `short_description` and `personalization` in {language_name}. "
        "Quotes stay in the original language.",
    ]
    for s in profile.signals:
        lines.append(f"- `{s.name}`: {s.description}")
    if profile.context:
        lines += ["", f"Who will contact the company: {profile.context}"]
    lines += ["", "Answer only with JSON matching the schema."]
    return "\n".join(lines)


@dataclass
class ClaudeResult:
    payload: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0
    error: str | None = None
    raw: str = ""
    cached: bool = False  # answer from cache: nothing was spent in this run


def auth_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for `claude -p` with exactly one credential.

    CLAUDE_CODE_OAUTH_TOKEN (a Claude subscription token from `claude setup-token`)
    wins when set: ANTHROPIC_API_KEY is removed, because Claude Code would
    otherwise prefer the key and bill the API account instead of the subscription.
    """
    env = dict(os.environ if base is None else base)
    # `docker run --env-file` keeps quotes around values (compose strips them).
    for k in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
        if k in env:
            env[k] = env[k].strip().strip("\"'")
    if env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        env.pop("ANTHROPIC_API_KEY", None)
    return env


def auth_mode(env: dict[str, str] | None = None) -> str:
    """'oauth', 'api_key' or '' — which credential `claude -p` will use."""
    env = auth_env(env)
    if env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "oauth"
    return "api_key" if env.get("ANTHROPIC_API_KEY") else ""


def claude_bin() -> str:
    found = shutil.which("claude")
    if not found:
        raise RuntimeError("`claude` is not on PATH. Install: npm i -g @anthropic-ai/claude-code "
                           "and set CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY.")
    return found


def empty_workdir() -> Path:
    """Working directory without CLAUDE.md, so nothing is auto-discovered."""
    d = Path(tempfile.gettempdir()) / "grizly-claude-empty"
    d.mkdir(exist_ok=True)
    return d


def missing_flags(help_text: str) -> list[str]:
    return [f for f in LEAN_FLAGS if f not in help_text]


def prompt_digest(instruction: str, schema: dict[str, Any]) -> str:
    """Changes when the question changes, so the cache never answers a different one."""
    h = hashlib.sha256(instruction.encode())
    h.update(json.dumps(schema, sort_keys=True).encode())
    return h.hexdigest()[:16]


def cache_path(cache_dir: Path, company: dict[str, str], pages: dict[str, str],
               digest: str) -> Path:
    h = hashlib.sha256()
    h.update(f"{company.get('country', '')}:{company.get('company_id', '')}:{digest}".encode())
    for url in sorted(pages):
        h.update(url.encode())
        h.update(pages[url][:4000].encode("utf-8", "replace"))
    return cache_dir / f"{h.hexdigest()[:32]}.json"


def page_blob(company: dict[str, str], pages: dict[str, str], id_label: str) -> str:
    blob = [f"# Company: {company.get('name', '')} ({id_label} {company.get('company_id', '')}, "
            f"{company.get('country', '')})"]
    if company.get("hq_city"):
        blob.append(f"Registered office: {company['hq_city']}, {company.get('hq_region', '')}")
    total = 0
    for url, text in pages.items():
        if total >= MAX_TOTAL_CHARS:
            break
        chunk = text[: MAX_TOTAL_CHARS - total]
        blob.append(f"\n\n## Page {url}\n{chunk}")
        total += len(chunk)
    return "\n".join(blob)


def call_claude(blob: str, *, instruction: str, schema: dict[str, Any], model: str,
                budget: float, timeout: int = 240) -> ClaudeResult:
    """One lean `claude -p` run: instruction as system prompt, page text on stdin."""
    cmd = [
        claude_bin(), "-p",
        "--system-prompt", instruction,
        "--output-format", "json",
        "--json-schema", json.dumps(schema, ensure_ascii=False),
        "--model", model,
        "--max-budget-usd", f"{budget:.2f}",
        "--tools", "",
        "--disallowed-tools", NO_TOOLS,
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--setting-sources", "",
    ]
    env = auth_env()
    env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    try:
        proc = subprocess.run(  # binary from shutil.which, no shell
            cmd, input=blob, capture_output=True, text=True, encoding="utf-8",
            timeout=timeout, env=env, cwd=empty_workdir())
    except subprocess.TimeoutExpired:
        return ClaudeResult(error=f"timeout {timeout}s")
    except OSError as e:
        return ClaudeResult(error=f"claude failed to start: {e}")
    return parse_output(proc.returncode, proc.stdout, proc.stderr)


def parse_output(returncode: int, stdout: str, stderr: str) -> ClaudeResult:
    if returncode != 0:
        msg = (stderr or stdout or "claude failed").strip()
        return ClaudeResult(error=msg[:500], raw=stdout)
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return ClaudeResult(error="output is not JSON", raw=stdout[:500])
    cost = float(envelope.get("total_cost_usd") or 0.0)
    if envelope.get("is_error"):
        return ClaudeResult(cost_usd=cost, raw=stdout, error=str(envelope.get("result"))[:300])
    payload = envelope.get("structured_output")
    if payload is None:
        result = envelope.get("result")
        if isinstance(result, dict):
            payload = result
        elif isinstance(result, str):
            try:
                payload = json.loads(result)
            except json.JSONDecodeError:
                return ClaudeResult(cost_usd=cost, raw=stdout[:500], error="result is not JSON")
        else:
            return ClaudeResult(cost_usd=cost, raw=stdout[:500], error="no structured_output")
    if not isinstance(payload, dict):
        return ClaudeResult(cost_usd=cost, raw=stdout[:500], error="payload is not an object")
    return ClaudeResult(payload=payload, cost_usd=cost)
