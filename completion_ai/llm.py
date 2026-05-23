"""Call Qwen (DashScope, OpenAI-compatible) for:
  1. discover_subcommands - given one help text, list direct subcommand names.
  2. extract_schema - given the full help tree, return structured CLI schema.
"""
from __future__ import annotations

import json
import os
import textwrap

from openai import OpenAI

from .crawler import HelpNode

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = os.environ.get("COMPLETION_AI_MODEL", "qwen-plus")


def _client() -> OpenAI:
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY is not set")
    base_url = os.environ.get("COMPLETION_AI_BASE_URL", DEFAULT_BASE_URL)
    return OpenAI(api_key=api_key, base_url=base_url)


DISCOVER_SYSTEM = textwrap.dedent(
    """
    You read one CLI's --help output and list its direct subcommand names.

    Output JSON exactly like: {"subcommands": ["name1", "name2"]}

    Rules:
    - Only direct subcommands (not flags, not nested subcommands).
    - For aliased entries like "plugin|plugins" return the canonical first
      name only ("plugin").
    - Strip placeholder tokens like "[options]", "[args]", "<target>", etc.
    - Skip "help" if listed as a subcommand.
    - If no subcommand section exists, return {"subcommands": []}.
    - Be conservative: only what is explicitly listed.
    - Do not invent commands not present in the help text.
    """
).strip()


EXTRACT_SYSTEM = textwrap.dedent(
    """
    You analyze CLI --help output and produce a JSON schema describing the
    command tree, flags, and positional arguments. Be conservative: only
    include things explicitly shown in the help text. Do not invent flags.

    Output JSON with this exact shape (no prose, no markdown fences):
    {
      "name": "<root command>",
      "description": "<one-line summary>",
      "flags": [
        {"long": "--foo", "short": "-f", "takes_value": true, "value_hint": "file|dir|enum|string", "choices": ["a","b"], "description": "..."}
      ],
      "positionals": [
        {"name": "PATH", "value_hint": "file|dir|string", "description": "...", "repeatable": false}
      ],
      "subcommands": [
        { ... same shape recursively, omit fields not present ... }
      ]
    }

    Rules:
    - "short" may be omitted if absent. "choices" only when help lists them.
    - "value_hint" must be one of: file, dir, command, host, user, branch,
      string, enum, integer. Use "string" when unsure.
    - Keep descriptions under 80 chars, single line.
    - Omit any field you are unsure about rather than guessing.
    - Include every subcommand whose help text appears in the input, even if
      its own help is sparse.
    """
).strip()


def discover_subcommands(help_text: str, model: str = DEFAULT_MODEL) -> list[str]:
    client = _client()
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": DISCOVER_SYSTEM},
            {"role": "user", "content": help_text},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content or "{}")
    raw = data.get("subcommands") or []
    out: list[str] = []
    seen: set[str] = set()
    for s in raw:
        if not isinstance(s, str):
            continue
        name = s.strip().split()[0] if s.strip() else ""
        if name and name not in seen and not name.startswith("-"):
            seen.add(name)
            out.append(name)
    return out


def _serialize_tree(node: HelpNode) -> str:
    parts = []
    for n in node.flatten():
        header = "$ " + " ".join(n.path) + " --help"
        parts.append(f"{header}\n{n.help_text.rstrip()}")
    return "\n\n---\n\n".join(parts)


def extract_schema(root: HelpNode, model: str = DEFAULT_MODEL, verbose: bool = False) -> dict:
    client = _client()
    user_content = (
        f"Root command: {root.path[0]}\n\n"
        f"Help outputs (root + subcommands):\n\n{_serialize_tree(root)}"
    )
    if verbose:
        print(f"[llm] extract model={model} prompt_chars={len(user_content)}")
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user", "content": user_content},
        ],
        temperature=0.1,
        response_format={"type": "json_object"},
    )
    content = resp.choices[0].message.content or "{}"
    return json.loads(content)
