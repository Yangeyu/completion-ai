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

## Benchmark

`eval/check_claude.py` 用 `claude --help` 及若干子命令的 help 文本作为
golden fixture，验证 `discover_subcommands` 和 `_extract_node` 的输出是
否符合预期。每次改 prompt 都应该跑一次。

```sh
# 用 uv（自动激活项目环境）
uv run python -m eval.check_claude

# 或直接用项目 venv
.venv/bin/python -m eval.check_claude
```

需要 `DASHSCOPE_API_KEY`。退出码 0/1，可直接接 CI。

样例输出：

```
=== claude ===
[discover] precision=1.00 recall=1.00 f1=1.00  -> PASS
[flags]    39/39 recall=1.00                   -> PASS
[positionals] prompt OK                        -> PASS
[details]  11/11 rate=1.00                     -> PASS

=== claude auth ===
[discover] precision=1.00 recall=1.00 f1=1.00  -> PASS
...

=== Summary ===
  PASS  claude
  PASS  claude auth
  PASS  claude mcp add
  overall: PASS
```

### 评估维度与阈值

| 维度 | 含义 | 阈值 |
|---|---|---|
| `discover` | 子命令集合 precision/recall/F1 | F1 ≥ 0.95 |
| `flags`    | must-have flag 命中率           | recall ≥ 0.90 |
| `positionals` | 名称与 `repeatable` 是否匹配 | 全中 |
| `details`  | 抽样 flag 的 short/takes_value/choices | 通过率 ≥ 0.85 |

### 扩展 fixture

加新用例只要两步，不用改脚本：

1. 把目标命令的 help 文本存到 `eval/fixtures/<name>_help.txt`
   ```sh
   docker buildx build --help > eval/fixtures/docker_buildx_build_help.txt
   ```
2. 在 `eval/fixtures/claude_expected.json` 的 `cases[]` 里追加一项：
   ```json
   {
     "path": ["docker", "buildx", "build"],
     "help_file": "docker_buildx_build_help.txt",
     "subcommands": [],
     "positionals": [{"name": "PATH"}],
     "must_have_flags": ["--tag", "--file", "--platform"],
     "flag_details": [
       {"long": "--tag", "short": "-t", "takes_value": true}
     ]
   }
   ```

## License

MIT
