"""completion-ai CLI entry point."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from rich.console import Console
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from . import __version__
from .crawler import HelpNode, run_help
from .llm import DEFAULT_MODEL, discover_subcommands, extract_schema
from .renderer import render_zsh

OMZ_PLUGIN_NAME = "completion-ai"
TOTAL_PHASES = 5


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
        "-d", "--depth", type=int, default=3,
        help=(
            "Max subcommand crawl depth (default: 3). Larger trees are "
            "slower: ~1 LLM call per node. Drop to 2 for a quick pass."
        ),
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
            f"'{OMZ_PLUGIN_NAME}'."
        ),
    )
    p.add_argument(
        "-f", "--force", action="store_true",
        help="Regenerate even if an output file already exists (skips reuse).",
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


def _count_schema(schema: dict) -> tuple[int, int]:
    """Return (total flags, total commands incl. root) in the schema tree."""
    flags = len(schema.get("flags") or [])
    cmds = 1
    for sub in schema.get("subcommands") or []:
        f, c = _count_schema(sub)
        flags += f
        cmds += c
    return flags, cmds


def _expand_nested(
    node: HelpNode,
    max_depth: int,
    depth: int,
    model: str,
    on_extra: callable,
) -> None:
    """For depth>2, fetch deeper levels under a node (no progress UI per level)."""
    if depth >= max_depth:
        return
    subs = discover_subcommands(node.help_text, model=model)
    on_extra(len(subs))
    for sub in subs:
        if not sub:
            continue
        path = node.path + [sub]
        child = HelpNode(path=path, help_text=run_help(path))
        node.children.append(child)
        _expand_nested(child, max_depth, depth + 1, model, on_extra)


def _phase_progress(console: Console) -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    )


def _run_with_progress(args, console: Console) -> tuple[HelpNode, dict, str, bool, str] | int:
    """Run the pipeline with phased progress UI. Returns ints on early-exit error."""
    cmd = args.command
    model = args.model

    # Phase 1: root --help
    with _phase_progress(console) as p:
        t = p.add_task(f"[cyan]\\[1/{TOTAL_PHASES}] Running `{cmd} --help`", total=1)
        try:
            root = HelpNode(path=[cmd], help_text=run_help([cmd]))
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        p.update(t, advance=1)

    # Phase 2: discover subcommands
    with _phase_progress(console) as p:
        t = p.add_task(
            f"[cyan]\\[2/{TOTAL_PHASES}] Discovering subcommands ({model})", total=1,
        )
        try:
            subs = discover_subcommands(root.help_text, model=model) if args.depth > 1 else []
        except Exception as e:
            print(f"error: LLM discover failed: {e}", file=sys.stderr)
            return 3
        p.update(t, advance=1)
    console.print(f"  [dim]→ discovered {len(subs)} subcommand(s)[/dim]")

    # Phase 3: fetch subcommand --help (total may grow if depth>2)
    with _phase_progress(console) as p:
        t = p.add_task(
            f"[cyan]\\[3/{TOTAL_PHASES}] Fetching subcommand help",
            total=max(len(subs), 1),
        )
        if not subs:
            p.update(t, advance=1)
        for sub in subs:
            if not sub:
                p.advance(t)
                continue
            child = HelpNode(path=[cmd, sub], help_text=run_help([cmd, sub]))
            root.children.append(child)
            if args.depth > 2:
                def _grow(n: int, _t=t, _p=p):
                    _p.update(_t, total=_p.tasks[_t].total + n)
                _expand_nested(child, args.depth, 2, model, _grow)
            p.advance(t)
    n_nodes = len(root.flatten())
    console.print(f"  [dim]→ fetched {n_nodes} help text(s)[/dim]")
    if n_nodes >= 50:
        est_min = max(1, n_nodes // 30)  # ~30 nodes/min at 8-way concurrency
        console.print(
            f"  [yellow]→ {n_nodes} nodes is large; schema extraction may "
            f"take ~{est_min}–{est_min * 2} min. Pass [bold]-d 2[/bold] "
            f"for a quicker pass.[/yellow]"
        )

    # Phase 4: extract schema
    with _phase_progress(console) as p:
        t = p.add_task(
            f"[cyan]\\[4/{TOTAL_PHASES}] Extracting schema ({model})", total=1,
        )
        try:
            schema = extract_schema(root, model=model, verbose=False)
        except Exception as e:
            print(f"error: LLM extract failed: {e}", file=sys.stderr)
            return 3
        p.update(t, advance=1)
    n_flags, n_cmds = _count_schema(schema)
    console.print(f"  [dim]→ extracted {n_flags} flag(s), {n_cmds} command(s)[/dim]")

    # Phase 5: render + validate
    with _phase_progress(console) as p:
        t = p.add_task(f"[cyan]\\[5/{TOTAL_PHASES}] Rendering & validating", total=1)
        if not schema.get("name"):
            schema["name"] = cmd
        script = render_zsh(schema)
        ok, syn_msg = True, ""
        if not args.no_syntax_check:
            ok, syn_msg = _syntax_check(script)
        p.update(t, advance=1)
    if not ok:
        console.print("  [yellow]→ zsh -n reported warnings[/yellow]")

    return root, schema, script, ok, syn_msg


def _run_plain(args, log) -> tuple[HelpNode, dict, str, bool, str] | int:
    """Verbose / non-TTY path: log each step plainly, no progress UI."""
    cmd = args.command
    model = args.model

    log(f"[1/{TOTAL_PHASES}] running `{cmd} --help`")
    try:
        root = HelpNode(path=[cmd], help_text=run_help([cmd]))
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    subs: list[str] = []
    if args.depth > 1:
        log(f"[2/{TOTAL_PHASES}] discovering subcommands (model={model})")
        try:
            subs = discover_subcommands(root.help_text, model=model)
        except Exception as e:
            print(f"error: LLM discover failed: {e}", file=sys.stderr)
            return 3
        log(f"discovered: {subs}")

    log(f"[3/{TOTAL_PHASES}] fetching {len(subs)} subcommand help(s)")
    for sub in subs:
        if not sub:
            continue
        child = HelpNode(path=[cmd, sub], help_text=run_help([cmd, sub]))
        root.children.append(child)
        if args.depth > 2:
            _expand_nested(child, args.depth, 2, model, lambda _n: None)

    n_nodes = len(root.flatten())
    log(f"collected {n_nodes} help section(s)")

    log(f"[4/{TOTAL_PHASES}] extracting schema")
    try:
        schema = extract_schema(root, model=model, verbose=True)
    except Exception as e:
        print(f"error: LLM extract failed: {e}", file=sys.stderr)
        return 3
    n_flags, n_cmds = _count_schema(schema)
    log(f"extracted: {n_flags} flag(s), {n_cmds} command(s)")

    if not schema.get("name"):
        schema["name"] = cmd
    script = render_zsh(schema)

    log(f"[5/{TOTAL_PHASES}] validating with `zsh -n`")
    ok, syn_msg = True, ""
    if not args.no_syntax_check:
        ok, syn_msg = _syntax_check(script)
    log(f"validation: {'ok' if ok else 'failed'}")

    return root, schema, script, ok, syn_msg


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except KeyboardInterrupt:
        print("\naborted by user", file=sys.stderr)
        return 130


def _main(argv: list[str] | None) -> int:
    args = build_parser().parse_args(argv)
    cmd = args.command

    console = Console(stderr=True)
    show_progress = not args.verbose and console.is_terminal

    def log(msg: str) -> None:
        if args.verbose:
            print(f"[completion-ai] {msg}", file=sys.stderr)

    # Fast path: reuse an existing completion file when present.
    out_candidate = args.output or Path.cwd() / f"_{cmd}"
    if out_candidate.exists() and not args.force:
        script = out_candidate.read_text()
        msg = f"reusing existing {out_candidate} (use --force to regenerate)"
        if show_progress:
            console.print(f"[green]✓[/green] {msg}")
        else:
            log(msg)
        if args.install:
            return _install_to_omz(cmd, script, log)
        print(f"wrote {out_candidate}")
        return 0

    if show_progress:
        result = _run_with_progress(args, console)
    else:
        result = _run_plain(args, log)

    if isinstance(result, int):
        return result
    root, schema, script, ok, syn_msg = result

    if args.dump_schema:
        args.dump_schema.write_text(json.dumps(schema, indent=2, ensure_ascii=False))
        log(f"wrote schema -> {args.dump_schema}")

    if not ok:
        print("warning: generated script failed `zsh -n` check:", file=sys.stderr)
        print(syn_msg, file=sys.stderr)
        print("output still written; please review.", file=sys.stderr)

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
