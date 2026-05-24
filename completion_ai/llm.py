"""Call Qwen (DashScope, OpenAI-compatible) for:
  1. discover_subcommands - given one help text, list direct subcommand names.
  2. extract_schema - given a help tree (built by the crawler), produce a
     CLI schema. Tree structure (names, parent/child links) comes from the
     crawler; the LLM only extracts each node's flags/positionals/description.
"""
from __future__ import annotations

import json
import os
import textwrap
from concurrent.futures import ThreadPoolExecutor

from openai import OpenAI

from .crawler import HelpNode

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = os.environ.get("COMPLETION_AI_MODEL", "qwen3.6-flash")


def _client() -> OpenAI:
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY is not set")
    base_url = os.environ.get("COMPLETION_AI_BASE_URL", DEFAULT_BASE_URL)
    return OpenAI(api_key=api_key, base_url=base_url)


DISCOVER_SYSTEM = textwrap.dedent(
    """
    Output format — MANDATORY:
    Return a JSON OBJECT with EXACTLY one key, "subcommands", whose value
    is an ARRAY of strings. Nothing else. No markdown, no prose, no
    extra keys, no top-level array.

    ✓ Correct:   {"subcommands": ["build", "run", "ps"]}
    ✓ Correct:   {"subcommands": []}
    ✗ Wrong:     ["build", "run", "ps"]
    ✗ Wrong:     {"commands": [...]}
    ✗ Wrong:     {"subcommands": [{"name": "build"}]}   (must be strings)

    Task:
    You read one CLI's --help output and list its direct subcommand names.

    A subcommand can ONLY come from a section explicitly headed
    "Commands:" or "Subcommands:" (case-insensitive). If no such section
    is present, return {"subcommands": []} — do NOT invent subcommands
    from any other text.

    Sections you MUST ignore:
    - "Usage:" — tokens here (e.g. "cmd|alias", "[command]", "<target>")
      are the command's own name, aliases, or placeholders, never its
      subcommands.
    - "Options:" / "Flags:" — those are flags, not subcommands.
    - "Examples:", "Description:", "Aliases:", or any free prose.

    Rules:
    - Only direct subcommands (not flags, not nested sub-subcommands).
    - For aliased entries inside the Commands: section like
      "plugin|plugins" or "update|upgrade", return EVERY name in the
      alias group as separate entries (e.g. both "plugin" and "plugins",
      both "update" and "upgrade"). They are interchangeable names for
      the same command and the user should be able to tab-complete any
      of them.
    - Strip placeholder tokens like "[options]", "[args]", "<target>".
    - Skip "help" if listed as a subcommand.
    - Be conservative: only what is explicitly listed under Commands:.
    - Do not invent commands not present in the help text.
    """
).strip()


EXTRACT_SYSTEM = textwrap.dedent(
    """
    Output format — MANDATORY:
    Return a single JSON OBJECT with EXACTLY these three keys:
    "description" (string), "flags" (array), "positionals" (array).
    No markdown, no prose, no extra keys, no top-level array.

    ✓ Correct:   {"description": "...", "flags": [...], "positionals": [...]}
    ✗ Wrong:     [{"long": "--foo"}, ...]
    ✗ Wrong:     {"name": "foo", "description": "..."}        (no "name")
    ✗ Wrong:     {"subcommands": [...], ...}                  (no "subcommands")

    Each flag object: {"long": "--foo", "short": "-f", "takes_value": true,
      "value_hint": "file|dir|enum|string", "choices": ["a","b"],
      "description": "..."}.
    Each positional object: {"name": "PATH", "value_hint": "file",
      "description": "...", "repeatable": false}.

    Task:
    You analyze a single CLI command's --help output and extract its flags
    and positional arguments. Be conservative: only include things
    explicitly shown in the help text. Do not invent flags.

    Rules:
    - Do NOT include a "name" field; the caller already knows the command.
    - Do NOT include a "subcommands" field; the caller owns the command
      tree. Even if the help text lists subcommands, ignore that section.
    - "short" may be omitted if absent. "choices" only when help lists them.
    - "value_hint" must be one of: file, dir, command, host, user, branch,
      string, enum, integer. Use "string" when unsure.
    - Keep descriptions under 80 chars, single line.
    - Omit any field you are unsure about rather than guessing.
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
        # Disable Qwen3 hybrid-thinking mode. Our task is structural pattern
        # extraction, not reasoning — thinking adds 3-5x latency per call
        # with zero quality benefit. No-op on non-Qwen3 models.
        extra_body={"enable_thinking": False},
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


EXTRACT_MAX_WORKERS = 8


def extract_schema(
    root: HelpNode, model: str = DEFAULT_MODEL, verbose: bool = False
) -> dict:
    """Extract a CLI schema from a help tree.

    Each node's flags/positionals/description are extracted independently
    by the LLM in parallel. The tree structure (command names and the
    parent/child layout) comes from the crawler's HelpNode tree, never
    from the LLM.
    """
    nodes = root.flatten()
    with ThreadPoolExecutor(max_workers=EXTRACT_MAX_WORKERS) as ex:
        results = list(ex.map(lambda n: _extract_node(n, model, verbose), nodes))
    fields = {id(n): r for n, r in zip(nodes, results, strict=True)}
    return _assemble(root, fields)


def _assemble(node: HelpNode, fields: dict[int, dict]) -> dict:
    f = fields[id(node)]
    return {
        "name": node.path[-1],
        "description": f.get("description", ""),
        "flags": f.get("flags") or [],
        "positionals": f.get("positionals") or [],
        "subcommands": [_assemble(c, fields) for c in node.children],
    }


def _extract_node(node: HelpNode, model: str, verbose: bool) -> dict:
    """One LLM call: extract flags/positionals/description for one node."""
    path = " ".join(node.path)
    user_content = (
        f"Command: {path}\n\n"
        f"--help output:\n\n{node.help_text.rstrip()}"
    )
    if verbose:
        print(f"[llm] extract {path!r} chars={len(user_content)}")
    resp = _client().chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user", "content": user_content},
        ],
        temperature=0.1,
        response_format={"type": "json_object"},
        extra_body={"enable_thinking": False},
    )
    if resp.choices[0].finish_reason == "length":
        raise ValueError(
            f"LLM response truncated for {path!r}; help text may be too long."
        )
    return json.loads(resp.choices[0].message.content or "{}")
