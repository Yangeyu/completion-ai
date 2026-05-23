"""Recursively collect --help text for a target CLI.

Subcommand discovery is delegated to a caller-supplied callback (typically an
LLM call) so this module doesn't need to understand help-format dialects.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class HelpNode:
    path: list[str]
    help_text: str
    children: list["HelpNode"] = field(default_factory=list)

    def flatten(self) -> list["HelpNode"]:
        out = [self]
        for c in self.children:
            out.extend(c.flatten())
        return out


DiscoverFn = Callable[[str], list[str]]


def run_help(argv: list[str], timeout: float = 8.0) -> str:
    try:
        r = subprocess.run(
            argv + ["--help"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError) as e:
        return f"[completion-ai: failed to run {' '.join(argv)} --help: {e}]"
    return (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")


def crawl(cmd: str, max_depth: int, discover: DiscoverFn) -> HelpNode:
    if shutil.which(cmd) is None:
        raise FileNotFoundError(f"command not found on PATH: {cmd}")
    root = HelpNode(path=[cmd], help_text=run_help([cmd]))
    if max_depth > 1:
        _expand(root, max_depth, 1, discover)
    return root


def _expand(node: HelpNode, max_depth: int, depth: int, discover: DiscoverFn) -> None:
    if depth >= max_depth:
        return
    for sub in discover(node.help_text):
        if not sub or not isinstance(sub, str):
            continue
        child_path = node.path + [sub]
        child = HelpNode(path=child_path, help_text=run_help(child_path))
        node.children.append(child)
        _expand(child, max_depth, depth + 1, discover)
