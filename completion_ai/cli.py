"""completion-ai CLI entry point."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

OMZ_PLUGIN_NAME = "completion-ai"

from . import __version__
from .crawler import crawl
from .llm import DEFAULT_MODEL, discover_subcommands, extract_schema
from .renderer import render_zsh


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="completion-ai",
        description="Generate zsh completion scaffolding for any CLI using an LLM.",
    )
    p.add_argument("command", help="Target CLI command (must be on PATH)")
    p.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Output path (default: ./_<command>)",
    )
    p.add_argument(
        "-d", "--depth", type=int, default=2,
        help="Max subcommand crawl depth (default: 2)",
    )
    p.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"LLM model id (default: {DEFAULT_MODEL}; env COMPLETION_AI_MODEL)",
    )
    p.add_argument(
        "--dump-schema", type=Path, default=None,
        help="Also write the intermediate JSON schema here",
    )
    p.add_argument(
        "--no-syntax-check", action="store_true",
        help="Skip the zsh -n syntax validation",
    )
    p.add_argument(
        "--install", action="store_true",
        help=(
            "Install the generated completion into the oh-my-zsh plugin "
            f"'{OMZ_PLUGIN_NAME}' (overrides --output)."
        ),
    )
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"completion-ai {__version__}")
    return p


def _syntax_check(script: str) -> tuple[bool, str]:
    with tempfile.NamedTemporaryFile("w", suffix=".zsh", delete=False) as f:
        f.write(script)
        path = f.name
    try:
        r = subprocess.run(
            ["zsh", "-n", path],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0, (r.stderr or r.stdout).strip()
    except FileNotFoundError:
        return True, "zsh not found, skipping syntax check"
    finally:
        Path(path).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cmd = args.command

    def log(msg: str) -> None:
        if args.verbose:
            print(f"[completion-ai] {msg}", file=sys.stderr)

    discover_calls = [0]

    def _discover(help_text: str) -> list[str]:
        discover_calls[0] += 1
        subs = discover_subcommands(help_text, model=args.model)
        log(f"discover #{discover_calls[0]}: {subs}")
        return subs

    try:
        log(f"crawling `{cmd} --help` (depth={args.depth}, model={args.model})")
        root = crawl(cmd, max_depth=args.depth, discover=_discover)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"error: crawl failed: {e}", file=sys.stderr)
        return 3

    n_nodes = len(root.flatten())
    log(f"collected {n_nodes} help section(s) via {discover_calls[0]} discover call(s)")

    try:
        log("extracting schema")
        schema = extract_schema(root, model=args.model, verbose=args.verbose)
    except Exception as e:
        print(f"error: LLM call failed: {e}", file=sys.stderr)
        return 3

    if not schema.get("name"):
        schema["name"] = cmd

    if args.dump_schema:
        args.dump_schema.write_text(json.dumps(schema, indent=2, ensure_ascii=False))
        log(f"wrote schema -> {args.dump_schema}")

    script = render_zsh(schema)

    if not args.no_syntax_check:
        ok, msg = _syntax_check(script)
        if not ok:
            print("warning: generated script failed `zsh -n` check:", file=sys.stderr)
            print(msg, file=sys.stderr)
            print("output still written; please review.", file=sys.stderr)
        else:
            log("zsh -n syntax check passed")

    if args.install:
        return _install_to_omz(cmd, script, log)

    out = args.output or Path.cwd() / f"_{cmd}"
    out.write_text(script)
    print(f"wrote {out}")
    print(
        "next steps:\n"
        f"  1. review {out}\n"
        f"  2. move to a directory on $fpath, e.g.:\n"
        f"       mkdir -p ~/.zsh/completions && mv {out} ~/.zsh/completions/\n"
        f"       # add to ~/.zshrc if not already:  fpath=(~/.zsh/completions $fpath)\n"
        f"  3. reload completions:  autoload -U compinit && compinit\n"
        "tip: use --install to drop it straight into oh-my-zsh."
    )
    return 0


def _omz_root() -> Path:
    return Path(os.environ.get("ZSH") or Path.home() / ".oh-my-zsh")


def _install_to_omz(cmd: str, script: str, log) -> int:
    omz = _omz_root()
    if not omz.is_dir():
        print(
            f"error: oh-my-zsh not found at {omz}. "
            "Set $ZSH or install oh-my-zsh first.",
            file=sys.stderr,
        )
        return 4

    plugin_dir = omz / "custom" / "plugins" / OMZ_PLUGIN_NAME
    plugin_dir.mkdir(parents=True, exist_ok=True)
    target = plugin_dir / f"_{cmd}"
    target.write_text(script)
    log(f"installed -> {target}")

    # Ensure the plugin has an entrypoint file so oh-my-zsh recognizes it.
    stub = plugin_dir / f"{OMZ_PLUGIN_NAME}.plugin.zsh"
    if not stub.exists():
        stub.write_text(
            "# Auto-generated by completion-ai.\n"
            "# Adds this directory to fpath so the _<cmd> files here are picked up.\n"
            "fpath=(${0:A:h} $fpath)\n"
        )
        log(f"created plugin stub -> {stub}")

    zshrc = Path.home() / ".zshrc"
    plugin_listed = False
    if zshrc.exists():
        try:
            plugin_listed = OMZ_PLUGIN_NAME in zshrc.read_text()
        except OSError:
            pass

    print(f"installed completion for `{cmd}` -> {target}")
    if not plugin_listed:
        print(
            "\nOne more step: add the plugin to ~/.zshrc, e.g.:\n"
            f"    plugins=(... {OMZ_PLUGIN_NAME})\n"
        )
    print(
        "Then reload:\n"
        "    rm -f ~/.zcompdump* && exec zsh"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
