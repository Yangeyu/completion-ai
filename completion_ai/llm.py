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
    List the direct subcommand names from a CLI's --help output.

    Return JSON: {"subcommands": ["name", ...]}

    Rules:
    - Only take names from a section whose heading contains
      "Commands" or "Subcommands" (case-insensitive, e.g.
      "Commands:", "Available Commands:", "SUBCOMMANDS").
      If no such section exists, return {"subcommands": []}.
    - Ignore Usage:, Options:, Flags:, Examples:, and prose.
    - For aliases like "plugin|plugins", emit each name separately.
    - Strip placeholders like [options], <target>, [command].
    - Skip any entry named exactly "help" — every CLI framework
      auto-injects it and it duplicates the --help flag.
    - Never invent names not literally present in the section.
    """
).strip()


EXTRACT_SYSTEM = textwrap.dedent(
    """
    Extract flags and positional arguments from one CLI command's
    --help output. Output feeds a zsh tab-completion script:
    missed flags hurt UX, invented flags break trust — when in
    doubt, omit.

    Return JSON:
    {
      "description": "<one line>",
      "flags": [
        {"long": "--foo", "short": "-f", "takes_value": true,
         "value_hint": "file", "choices": ["a","b"],
         "description": "...", "repeatable": false}
      ],
      "positionals": [
        {"name": "PATH", "value_hint": "file",
         "description": "...", "repeatable": false}
      ]
    }

    Rules:
    - Every flag and positional MUST be a JSON object with the
      fields above. NEVER emit a bare string like "--foo" inside
      the arrays — wrap it as {"long": "--foo", ...}.
    - Only include flags/positionals literally shown in the help.
    - Omit "short", "choices", "repeatable" when not applicable.
    - value_hint: file, dir, path, port, url, host, user, branch,
      duration, integer, string, enum. Use "string" if unsure.
    - Set repeatable: true when help shows "..." or says
      "may be repeated / multiple times".
    - Treat --no-xxx / --disable-xxx as standalone flags
      (takes_value: false).
    - Keep descriptions to one line, max 120 chars. Prefer the
      help's own wording; truncate at a word boundary if longer.
    - Do not emit "name" or "subcommands" fields.
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


FALLBACK_MODEL = os.environ.get("COMPLETION_AI_FALLBACK_MODEL", "qwen3.6-plus")


def _call_extract(path: str, help_text: str, model: str, max_tokens: int):
    user_content = f"Command: {path}\n\n--help output:\n\n{help_text.rstrip()}"
    return _client().chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user", "content": user_content},
        ],
        temperature=0,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
        extra_body={"enable_thinking": False},
    )


def _extract_node(node: HelpNode, model: str, verbose: bool) -> dict:
    """One LLM call: extract flags/positionals/description for one node.

    If the primary model truncates (commands like `podman run` have ~100
    flags and overflow 8K output), retry once with FALLBACK_MODEL at a
    higher token budget. Only skip the node if both attempts truncate.
    """
    import sys
    path = " ".join(node.path)
    if verbose:
        print(f"[llm] extract {path!r} chars={len(node.help_text)}")

    resp = _call_extract(path, node.help_text, model, max_tokens=8192)
    if resp.choices[0].finish_reason == "length":
        print(f"[completion-ai] {path!r} truncated on {model}, "
              f"retrying with {FALLBACK_MODEL}", file=sys.stderr)
        resp = _call_extract(path, node.help_text, FALLBACK_MODEL, max_tokens=16384)
        if resp.choices[0].finish_reason == "length":
            print(f"[completion-ai] WARN {path!r} still truncated on "
                  f"{FALLBACK_MODEL}; skipping flags for this node",
                  file=sys.stderr)
            return {"description": "", "flags": [], "positionals": []}

    return _normalize(json.loads(resp.choices[0].message.content or "{}"))


def _normalize(data: dict) -> dict:
    """Coerce LLM output into the schema shape downstream code expects.

    At scale (1000+ nodes) Qwen-flash occasionally returns a bare string
    like "--foo" instead of {"long": "--foo"} despite explicit prompt
    rules. Salvage what we can here so renderer/assembler can trust input.
    """
    def to_flag(x):
        if isinstance(x, dict):
            return x
        if isinstance(x, str):
            s = x.strip()
            if s.startswith("--"):
                return {"long": s}
            if s.startswith("-") and len(s) > 1:
                return {"short": s}
        return None

    def to_pos(x):
        if isinstance(x, dict):
            return x
        if isinstance(x, str) and x.strip():
            return {"name": x.strip()}
        return None

    return {
        "description": data.get("description") or "",
        "flags":       [f for f in (to_flag(x) for x in (data.get("flags") or [])) if f],
        "positionals": [p for p in (to_pos(x)  for x in (data.get("positionals") or [])) if p],
    }
