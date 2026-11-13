[中文](README.md) | English

# zMAHarness

zMAHarness is a Skill plus a CLI tool that lets your AI agent (Codex or Claude Code), with your consent, ask other models to review, give a second opinion, or research, and then has the controller verify the results itself.

zMAHarness follows a **Single-Agent First** workflow. The controller agent (Codex Sol High by default for daily work) normally completes tasks on its own. When a critical change would benefit from an independent review, the controller proposes a collaboration and dispatches work only after you agree in the conversation. The controller independently checks the worker's results, decides which suggestions to adopt, and continues the original task.

## Requirements

- **Operating system**　Windows 11.
- **Controller**　Either [Codex Desktop](https://openai.com/codex/) or [Claude Code](https://code.claude.com/docs/).
- **Worker**　Install the Antigravity CLI (`agy`) from the [official Google documentation](https://antigravity.google/docs/install?tab=cli) and sign in with a personal Google AI Pro subscription.
- **Local tools**　Git, PowerShell 5.1 or 7, Python 3.11 or later. Add the `python.exe` you intend to use to PATH, and enable the Python user site-packages directory.

## Worker acceptance status

| Worker | Acceptance scope |
| --- | --- |
| Gemini | **Real dispatch**: T5 triggered a cross-project Second Opinion on BatchTable, requesting `gemini-3.8-flash-high · high` via `agy-print` in Context-Bundle mode. The controller adopted the suggestions after independent checking, and the ledger recorded `SUCCEEDED`/`ACCEPTED`. **Offline tests**: re-collection and the Verifier are validated against a de-identified real receipt from a Flash Low Review. Real end-to-end flows for other models and effort levels, the Research template, and Disposable Worktree have not been accepted yet. |
| Claude | Sonnet and Opus are listed as candidate models for `agy-print`. Real worker dispatch has not been accepted yet. |
| Codex | JSONL, Thread ID, and UTF-8 of the nested CLI have been verified. Tool operations in that run were refused by policy, and the formal `codex-exec` Adapter has not been integrated or accepted yet. |
| Terra GUI | V1 file handoff was once tested in an isolated environment. Formal V2 GUI dispatch has not been accepted yet. |

The model IDs listed in the Skill follow `agy models` as of November 2026 and may change with the CLI or with the models available to your account.

## Quick start in three steps

```powershell
# 1. Clone and enter the project
git clone https://github.com/zm0204/zMAHarness.git
Set-Location zMAHarness

# 2. Install for the current Windows user
.\setup.ps1

# 3. Check the environment
.\doctor.ps1
```

Setup reuses V1's `.local/settings.json` and working directory, and creates the V2 local configuration. It registers the engine path, written as an ASCII-escaped literal, in `zmaharness.pth` under the current Python's user site-packages directory, so clone paths containing Chinese characters are supported. The full Skill (including `references/`) is installed to both of the following locations.

- `%USERPROFILE%\.agents\skills\zmaharness\`
- `%USERPROFILE%\.claude\skills\zmaharness\`

Re-running keeps the existing TOML and V1 local settings, and updates the engine registration and both Skill copies. Installation needs only standard user privileges, and the Python side uses only the standard library. Doctor can check offline the CLI import, hints of an `agy.exe` installation, the local TOML, and the consistency of both Skill copies. Actual sign-in, model availability, and Skill loading in the controller UI must be confirmed when you use it.

The Skill was previously named `harness`. When setup is re-run, an old `harness` directory in either location above is removed automatically if it is confirmed to be an old install of this project. A Skill with the same name that does not belong to this project is always kept, and only a warning is printed.

## Collaborating in any project

After installation, open the Git project you want to work on with your chosen controller. Invoke it with `$zmaharness` in Codex Desktop, or `/zmaharness` in Claude Code. You can also simply assign development work; when the collaboration conditions are met, the controller shows a **[zMAHarness Collaboration Proposal]** card in the conversation (shown as 「【zMAHarness 協作建議】」 in a Chinese environment), listing the trigger, the suggested model, the task and scope, the authorization limits, and the reason for collaborating. The card and replies use the language you asked in; the brand, model IDs, and command names stay in their original form.

- **Decline**　The controller immediately continues the original task by itself and will not ask again about the same issue.
- **Continue collaborating**　You can choose the suggested model or the alternative model, or type to adjust the model or effort level, or to narrow the file scope. The controller prepares the data and a single-use Grant according to your choice, dispatches the work, verifies the result independently, and decides whether to adopt it.

`python -B -m mah --help` can be run from other project directories. Both `--repo` and the dispatch card's `repo_root` should point to **the root of the target Git project you are currently working on**. The zMAHarness clone directory is where the Python engine lives.

| Review mode | When to use |
| --- | --- |
| **Context-Bundle** | 1–2 core files can be packaged in full within the default 32 KB task limit. Complete uncommitted files can be reviewed. |
| **Disposable Worktree** | Multiple files, large files, or when related files need to be traced. The snapshot includes uncommitted changes and new files that match the rules, and if necessary creates a local anonymous commit only on a detached HEAD. |

Both modes bind the approved scope and keep receipts. Even after verification succeeds, the controller still decides whether to adopt the result. If the submission state is unknown, check the existing receipts and results first. The worker and the controller use the same Windows account; the read-only restriction relies on the CLI's session settings, and tool behavior has not been fully verified at the OS level. See the [collaboration rules](.agents/skills/zmaharness/SKILL.md) and [usage risks](DISCLAIMER.md).

## Local configuration and troubleshooting

- **Configuration location**: `%LOCALAPPDATA%\zMAHarness\harness.local.toml`, with default subdirectories `state`, `worktrees`, and `gui`. `agy-print` and in-conversation approval (`codex_prompt`) are enabled by default. CLI sessions always disable AI Credits and skip quota checks before and after dispatch.
- **Existing installations**: Until the new configuration exists, `mah` falls back to the old `%LOCALAPPDATA%\MultiAgentHarness\harness.local.toml` (if present). After running the new setup, the new configuration takes precedence. Old Ledgers and Runs stay where they are; to query or finish an old Run, pass `--config <old config file>` before the subcommand.
- **`mah` not found in other projects**: Make sure that terminal uses the same Python as the one used during installation, and that the Python user site-packages directory is enabled. Re-run setup after moving the clone or changing Python. You can also temporarily add the engine root to `PYTHONPATH`.
- **Skill not found**: Restart the controller app or start a new conversation, then type `$zmaharness` in Codex Desktop or `/zmaharness` in Claude Code.
- **`agy` not found**: Doctor looks in PATH and in the standard install location `%LOCALAPPDATA%\agy\bin\agy.exe`. For a custom installation, set `agy_command` in the local TOML.
- **PowerShell cannot run scripts**: Check the source of the scripts and the execution policy of the machine. If allowed, you can run `powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\setup.ps1` for a single process, and run Doctor the same way.

### Optional lightweight controller settings

```toml
[controller]
light_models = []
suggested_controller = "Codex Sol High"
```

`light_models` is a list of model ID strings and defaults to an empty list. Only when the controller confirms that its current model ID exactly matches one of the entries does it offer the lightweight-controller option on T1, T2, or T5 cards. `suggested_controller` is the name of the controller to suggest switching to, defaulting to `Codex Sol High`; you perform the switch in the controller UI.

## Roadmap

The following items are **planned and not yet implemented**.

2) Adapter interface refactoring.
3) `codex-exec` (GPT) and `claude-print` (Claude CLI) workers.
4) OpenAI-compatible API worker (DeepSeek, and local models such as Ollama). Supports Context-Bundle only, and requires users to provide their own API key.

## Offline verification

Run the following commands from the root of the zMAHarness clone. The tests use synthetic data and local stand-ins.

```powershell
python -B -m unittest -q
python -B -m unittest discover -s tests/v2 -q
```

For V1 file handoff and interruption recovery rules, see the [role rules](ROLES.md) and the [V1 inventory](docs/v2/V1_INVENTORY.md). Keep local settings, SQLite, task records, and private verification reports on your machine. The Git whitelist only includes project code, general documentation, and de-identified test data.

License: MIT, see [LICENSE](LICENSE). Please also see [DISCLAIMER.md](DISCLAIMER.md).
