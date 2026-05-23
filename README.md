# completion-ai

LLM-powered zsh completion scaffolding for arbitrary CLIs. Point it at any
command on your `PATH` and it will:

1. Recursively run `<cmd> --help` (and discovered subcommand `--help`s).
   Subcommand discovery is done by the LLM, not regex — so `install [options]`,
   `plugin|plugins`, and other oddly-formatted entries are picked up.
2. Ask Qwen (via DashScope OpenAI-compatible API) to extract a structured
   schema of flags, subcommands, and positional arguments.
3. Render a `#compdef` zsh completion script using a deterministic template
   (the LLM never writes shell directly).
4. Run `zsh -n` against the result and warn on syntax errors.

The output is meant to be **reviewed by a human** before installation — it's
a scaffold, not a runtime completion engine.

## Install

Recommended (global CLI, isolated venv managed by uv):

```sh
uv tool install -e .
```

Other options:

```sh
uv pip install -e .       # into the current uv venv
pip install -e .          # into the active python (conda, system, ...)
```

Requires `DASHSCOPE_API_KEY` in the environment.

## Usage

```sh
completion-ai claude                     # writes ./_claude
completion-ai claude --install           # install straight into oh-my-zsh
completion-ai claude -o ~/.zsh/completions/_claude
completion-ai docker --depth 3 -v        # crawl deeper, verbose
completion-ai gh --dump-schema gh.json   # also save intermediate schema
```

### Options

| Flag | Default | Notes |
|---|---|---|
| `-o, --output PATH` | `./_<cmd>` | Where to write the completion script |
| `-d, --depth N` | `2` | How deep to crawl subcommand help |
| `--model ID` | `qwen-plus` | Override via `COMPLETION_AI_MODEL` too |
| `--dump-schema PATH` | — | Also save the JSON the LLM produced |
| `--no-syntax-check` | off | Skip `zsh -n` validation |
| `--install` | off | Install into `$ZSH/custom/plugins/completion-ai/` |
| `-v, --verbose` | off | Progress logs to stderr |

### Environment

- `DASHSCOPE_API_KEY` — required
- `COMPLETION_AI_MODEL` — defaults to `qwen-plus`
- `COMPLETION_AI_BASE_URL` — defaults to DashScope OpenAI-compatible endpoint
- `ZSH` — oh-my-zsh root, used by `--install` (defaults to `~/.oh-my-zsh`)

## Installing the generated completion

### Option A — oh-my-zsh users (recommended)

```sh
completion-ai claude --install
```

This creates `$ZSH/custom/plugins/completion-ai/_claude` plus a
plugin stub. **First time only**, add the plugin to `~/.zshrc`:

```zsh
plugins=(git ... completion-ai)
```

Then refresh:

```sh
rm -f ~/.zcompdump* && exec zsh
```

Subsequent `completion-ai <cmd> --install` calls drop new completions into the
same plugin directory — no zshrc edits needed.

### Option B — plain zsh

```sh
completion-ai claude -o ~/.zsh/completions/_claude

# in ~/.zshrc (one-time):
fpath=(~/.zsh/completions $fpath)
autoload -U compinit && compinit
```

## How it works

```
[crawler]   run `<cmd> --help`
    ↓
[llm]       discover subcommand names from help text  (1 call per node)
    ↓
[crawler]   recursively run discovered subcommands' --help
    ↓
[llm]       extract structured JSON schema from all help texts  (1 call)
    ↓
[renderer]  JSON → #compdef zsh template (deterministic, not LLM)
    ↓
[validator] zsh -n syntax check
```

For `depth=2`, that's **2 LLM calls** total (1 discover + 1 extract).

## Limitations

- Static only: dynamic completions (e.g. `git checkout <branch>`) are not
  generated — the script may suggest a hint type (`branch`, `host`, ...) but
  won't query live state.
- The LLM may miss hidden flags or mislabel value hints. Always diff against
  `<cmd> --help` before trusting.
- Only zsh for now.

## Development

```sh
git clone <repo> && cd completion-ai
uv venv && source .venv/bin/activate
uv pip install -e .
```

## License

MIT
