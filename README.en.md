[中文](README.md) | English

# zMAHarness

zMAHarness is a Skill plus a CLI tool that lets your AI agent (Codex or Claude Code), with your consent, ask other models to review, give a second opinion, or research, and then has the controller verify the results itself.

zMAHarness follows a **Single-Agent First** workflow. The controller agent (Codex Sol High by default for daily work) normally completes tasks on its own. When a critical change would benefit from an independent review, the controller proposes a collaboration and dispatches work only after you agree in the conversation. The controller independently checks the worker's results, decides which suggestions to adopt, and continues the original task.

## Requirements

- **Operating system** Windows 11.
- **Controller** Either [Codex Desktop](https://openai.com/codex/) or [Claude Code](https://code.claude.com/docs/).
- **Gemini Worker (default)** Install the Antigravity CLI (`agy`) from the [official Google documentation](https://antigravity.google/docs/install?tab=cli) and sign in with a personal Google AI Pro subscription.
- **Claude Worker (optional)** Install the Claude Code CLI with `npm install -g @anthropic-ai/claude-code`, then sign in with your Claude account by running `claude auth login` (or run `claude` and follow the prompts). zMAHarness passes no API key variable to the Worker (it removes every `ANTHROPIC_` variable), and before showing a card it runs `claude auth status` to confirm that the CLI is signed in with a Claude subscription account; no card is shown when the CLI is signed out, the sign-in has expired, or it uses an API key, `apiKeyHelper`, or a third-party provider. With both checks in place, the Worker uses your Claude subscription and shares the same usage limit as your everyday use of Claude; actual billing still follows your Anthropic account settings. Running `claude` yourself in a terminal is not covered by these checks. Residual risk: your user-level `~/.claude/CLAUDE.md` and user settings such as hooks in `~/.claude/settings.json` are still loaded into the Worker and no flag turns them off; keep them free of anything that writes files, reaches the network, or changes instructions. zMAHarness only launches the native `claude.exe` inside the package, never through npm's `claude.cmd` shim.
- **GPT Worker (optional)** Install the Codex CLI with `npm install -g @openai/codex`, then sign in with your ChatGPT account by running `codex login`. zMAHarness passes no API key variable to the Worker (it removes every `OPENAI_` variable and every `CODEX_` variable except `CODEX_HOME`), and before showing a card it runs `codex login status` to confirm that the CLI is signed in with a ChatGPT account; no card is shown when the CLI is signed out or signed in with an API key. With both checks in place, the Worker uses your ChatGPT subscription and shares the same usage limit as your everyday use of Codex (`codex-exec` and Codex Desktop draw on the same ChatGPT quota, so the Worker fails once Desktop has used it up); actual billing still follows your OpenAI account settings. A tool item in JSONL makes the result ineligible for acceptance. `codex-exec` **supports Context-Bundle only**. GPT code-mode models may see `functions.exec` and `functions.wait`, whose execution backend must be disabled. Tool declarations are probed automatically before each dispatch, and unknown tools registered by a newer CLI are refused. Models that passed the probe on codex-cli 0.169.0: `gpt-6.1-sol`, `gpt-6.1-terra`, `gpt-6-astra`, `gpt-6-sol`, `gpt-6-luna`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna` (the models your account can use are listed by `codex debug models`; see the probe table in the [adapter documentation](docs/ADAPTERS.md#codex-exec)). If Codex exposes an official no-tools entry point, it will replace this check. Codex may still load global `AGENTS.md` or refresh model metadata during a real run. See the [adapter documentation](docs/ADAPTERS.md#codex-exec). Last tested version: codex-cli 0.169.0 (on 2026-11-15 one smoke each with `gpt-6.1-terra` and `gpt-6.1-sol`: all five tool probes failed and the JSONL had no tool execution items, see the [smoke record](docs/v2/CODEX_SMOKE_TEST.md)). The version is not pinned: after an upgrade, the feature and tool declaration checks run before each dispatch and refuse it if they fail. zMAHarness only launches the native `codex.exe` inside the package, never through npm's `codex.cmd` shim.
- **Local tools** Git, PowerShell 5.1 or 7, Python 3.11 or later. Add the `python.exe` you intend to use to PATH, and enable the Python user site-packages directory.

In a regular PowerShell terminal, run `winget install -e --id Python.Python.3.13`, or install from python.org and select **Add python.exe to PATH**. If `python.exe` points into `Microsoft\WindowsApps` and cannot run, it is a Microsoft Store placeholder, not a working Python installation. Disable the `python.exe` and `python3.exe` aliases under Settings → Apps → Advanced app settings → App execution aliases, then reopen the terminal before installing.

## Worker acceptance status

| Worker | Acceptance scope |
| --- | --- |
| Gemini | **Real dispatch**: T5 triggered a cross-project Second Opinion on the author's real Python project (a table-comparison tool), requesting `gemini-3.8-flash-high · high` via `agy-print` in Context-Bundle mode. The controller adopted the suggestions after independent checking, and the ledger recorded `SUCCEEDED`/`ACCEPTED`. **Offline tests**: re-collection and the Verifier are validated against a de-identified real receipt from a Flash Low Review. Real end-to-end flows for other models and effort levels, the Research template, and Disposable Worktree have not been accepted yet. |
| Claude | Sonnet and Opus are listed as candidate models for `agy-print`. Real worker dispatch has not been accepted yet. |
| Claude (`claude-print`) | **Real dispatch accepted**: the author's real Python project (a table-comparison tool), `sonnet · medium`, Context-Bundle, `SUCCEEDED`/`ACCEPTED`. |
| GPT (`codex-exec`) | **Real dispatch accepted**: the author's real Python project (a table-comparison tool), Claude Code as the controller, `gpt-5.6-terra · medium`, Context-Bundle, `SUCCEEDED`/`ACCEPTED`. Context-Bundle only. |
| Terra GUI | V1 file handoff was once tested in an isolated environment. Formal V2 GUI dispatch has not been accepted yet. |

The model IDs listed in the Skill follow `agy models` as of November 2026 and may change with the CLI or with the models available to your account.

## Terms

| Term | Meaning |
| --- | --- |
| Controller | The AI agent you normally talk to (Codex Desktop or Claude Code). It owns the original task, proposes collaboration, and verifies the Worker's results. |
| Worker | Another model that receives the dispatched work. It runs through a local CLI (`agy`, `claude`, `codex`) and performs read-only analysis only. |
| Adapter | The zMAHarness module that launches one kind of Worker CLI: `agy-print` (Gemini), `claude-print` (Claude), `codex-exec` (GPT). |
| Card | The collaboration proposal the controller shows in the conversation, listing the model, task, and scope, and waiting for your consent. |
| Grant | The authorization record created when you agree to a card. It binds the scope and the number of dispatches. |
| Run | One dispatch. Each Run has its own Run ID, receipts, and result. |
| Ledger | A local SQLite database that records cards, Grants, Runs, and acceptance status. |
| T1–T5 | The five triggers for proposing collaboration: T1 the same error still fails after several different fixes; T2 a change touches high-risk paths and has a large diff; T3 a lot of reading is needed for a small output; T4 the controller's quota usage is high; T5 you ask for collaboration yourself. Thresholds are in `[triggers]` in the local TOML. |

## Quick start in three steps

Run `setup.ps1` in a regular PowerShell terminal, not in an AI agent's restricted sandbox. Setup writes to the current user's Python site-packages and Skill directories.

```powershell
# 1. Clone and enter the project
git clone https://github.com/zm0204/zMAHarness.git
Set-Location zMAHarness

# 2. Install for the current Windows user
.\setup.ps1

# 3. Check the environment
.\doctor.ps1
```

Setup creates `.local/settings.json` and a working directory inside the clone (used by the V1 file handoff flow; reused if they already exist, and never tracked by Git), and creates the local configuration `%LOCALAPPDATA%\zMAHarness\harness.local.toml`. It registers the engine path, written as an ASCII-escaped literal, in `zmaharness.pth` under the current Python's user site-packages directory, so clone paths containing Chinese characters are supported. The full Skill (including `references/`) is installed to both of the following locations.

- `%USERPROFILE%\.agents\skills\zmaharness\`
- `%USERPROFILE%\.claude\skills\zmaharness\`

Re-running keeps the existing TOML and V1 local settings, and updates the engine registration and both Skill copies. Installation needs only standard user privileges, and the Python side uses only the standard library. Doctor can check offline the CLI import, hints of an `agy.exe` installation, the local TOML, and the consistency of both Skill copies. Actual sign-in, model availability, and Skill loading in the controller UI must be confirmed when you use it.

Each Doctor line starts with `[通過]` (pass), `[警告]` (warning), `[需人工驗證]` (needs manual check), or `[失敗]` (fail); Doctor output is in Chinese. Only `[失敗]` makes the exit code 1. `[需人工驗證]` marks items Doctor cannot confirm offline (such as sign-in state) and does not mean the installation is broken. An optional Worker that is not installed shows as `[需人工驗證]`; ignore it if you do not use that Worker. The final section, 「V1 檔案交接（舊版相容）」 (V1 file handoff, legacy), only concerns the old GUI file handoff flow.

An existing local TOML keeps its `adapters.enabled` list and never gains new Workers automatically. To use the Claude Worker, add `"claude-print"` to `adapters.enabled` yourself; Doctor shows the line to use when it finds the claude executable but the Worker is not enabled.

The Skill was previously named `harness`. When setup is re-run, an old `harness` directory in either location above is removed automatically if it is confirmed to be an old install of this project. A Skill with the same name that does not belong to this project is always kept, and only a warning is printed.

## Collaborating in any project

After installation, open the Git project you want to work on with your chosen controller. Invoke it with `$zmaharness` in Codex Desktop, or `/zmaharness` in Claude Code. You can also simply assign development work; when the collaboration conditions are met, the controller shows a **[zMAHarness Collaboration Proposal]** card in the conversation (shown as 「【zMAHarness 協作建議】」 in a Chinese environment), listing the trigger, the suggested model, the task and scope, the authorization limits, and the reason for collaborating. The card and replies use the language you asked in; the brand, model IDs, and command names stay in their original form.

Before showing a card, the controller runs the read-only `mah preflight` check to confirm that the target is a local Git project, the local settings exist, and the Worker is enabled and its executable can be found; for the Claude and GPT Workers it also confirms that the CLI is signed in with a subscription account. If the check fails, no card is shown; the controller explains why and continues the original task by itself. A "not a Git project" or "cannot check the Git project" message can also mean that the target is outside the controller's current working folder or that the sandbox blocks access to it; open the controller in that project and try again.

- **Decline** The controller immediately continues the original task by itself and will not ask again about the same issue.
- **Continue collaborating** You can choose the suggested model or the alternative model, or type to adjust the model or effort level, or to narrow the file scope. The controller prepares the data and a Grant according to your choice, dispatches the work, verifies the result independently, and decides whether to adopt it. In-conversation approval (`codex_prompt`) always allows exactly 1 dispatch per card; terminal approval (`manual_cli`) allows a Disposable Worktree card up to `limits.max_dispatches_per_grant`, and a Context-Bundle card allows 1 dispatch in either mode.

`python -B -m mah --help` can be run from other project directories. Both `--repo` and the dispatch card's `repo_root` should point to **the root of the target Git project you are currently working on**. The zMAHarness clone directory is where the Python engine lives.

| Review mode | When to use |
| --- | --- |
| **Context-Bundle** | 1–2 core files can be packaged in full within the default 32 KB task limit. Complete uncommitted files can be reviewed. |
| **Disposable Worktree** | Multiple files, large files, or when related files need to be traced. The snapshot includes uncommitted changes and new files that match the rules, and if necessary creates a local anonymous commit only on a detached HEAD. Project AI settings (`.claude/`, `CLAUDE.md`, `CLAUDE.local.md`, `.mcp.json`, `AGENTS.md`, `AGENTS.override.md`, `.agents/`, `.agent/`, `.gemini/`, `GEMINI.md`, at any depth) are left out of the snapshot, so the Worker never sees them. |

Both modes bind the approved scope and keep receipts. Even after verification succeeds, the controller still decides whether to adopt the result. If the submission state is unknown, check the existing receipts and results first. The worker and the controller use the same Windows account; the read-only restriction relies on the CLI's session settings, and tool behavior has not been fully verified at the OS level. See the [collaboration rules](.agents/skills/zmaharness/SKILL.md) and [usage risks](DISCLAIMER.md).

### Trust model for consent

- **In-conversation approval (`codex_prompt`, default)**: after you agree in the controller conversation, the controller runs `mah grant approve --via codex_prompt` to record your consent. That record is written by the controller; the CLI can check the card hash, scope, and count, but it cannot stop a controller that does not follow the Skill from writing it.
- **Terminal approval (`manual_cli`)**: if you need a hard guarantee, set `approval.mode = "manual_cli"` in the local TOML. Every card must then be approved by you in an interactive terminal with `mah grant approve <card_id> --sha <full SHA-256> --via manual_cli`, typing the confirmation code shown on screen; `--via codex_prompt` is refused with exit code 10. The default `codex_prompt` mode accepts both.
- `delegate-implement` (letting a Worker change code) is a deferred feature; every Worker currently refuses it and only performs read-only analysis.

## Local configuration and troubleshooting

- **Configuration location**: `%LOCALAPPDATA%\zMAHarness\harness.local.toml`, with default subdirectories `state`, `worktrees`, and `gui`. `agy-print` and in-conversation approval (`codex_prompt`) are enabled by default. CLI sessions always disable AI Credits and skip quota checks before and after dispatch.
- **Cleaning up old data**: `privacy.retain_run_dirs_days` is not implemented yet and nothing is cleaned up automatically. Delete old data you no longer need under `state\runs` and `worktrees` in the configuration folder by hand; remove a Disposable Worktree with `git worktree remove <path>` in the target project, or run `git worktree prune` there if you already deleted the folder.
- **Before cleaning up `state\runs`**: run `mah status --active` first and make sure no Run is unfinished. Deleting the receipts of an unfinished Run keeps it from being collected normally; if a Run is stuck (for example, every dispatch is refused with exit code 13), end it with `mah resolve <run_id> --mark-failed --reason "<reason>"`. The Run becomes `FAILED` and the Grant is not refunded.
- **codex-exec is always refused with exit code 17 and the message mentions system Codex configuration or Windows Codex policy**: when `C:\ProgramData\OpenAI\Codex\config.toml`, `requirements.toml`, or the `RequirementsPath` value under `HKLM\SOFTWARE\Policies\OpenAI\Codex` exists, Codex applies these system-level settings, which `--ignore-user-config` cannot block and which may change the model provider, model catalog, or tools, so zMAHarness always refuses. If you created these files yourself, remove or rename them and retry; if your organization manages them, do not work around them and use another Worker.
- **Removing zMAHarness**: Delete `zmaharness.pth` from the Python user site-packages directory (find it with `python -m site --user-site`), the two `zmaharness` Skill directories listed above, and `%LOCALAPPDATA%\zMAHarness`, then delete the clone.
- **Worker executables**: zMAHarness looks for `claude`, `codex`, and `agy` only in absolute PATH entries and never uses a file of the same name in the current directory or a relative path; an executable that resolves inside the target project, a worktree, or a Bundle directory is always refused (exit code 14).
- **Existing installations**: Until the new configuration exists, `mah` falls back to the old `%LOCALAPPDATA%\MultiAgentHarness\harness.local.toml` (if present). After running the new setup, the new configuration takes precedence. Old Ledgers and Runs stay where they are; to query or finish an old Run, pass `--config <old config file>` before the subcommand.
- **`mah` not found in other projects**: Make sure that terminal uses the same Python as the one used during installation, and that the Python user site-packages directory is enabled. Re-run setup after moving the clone or changing Python. You can also temporarily add the engine root to `PYTHONPATH`.
- **Skill not found**: Restart the controller app or start a new conversation, then type `$zmaharness` in Codex Desktop or `/zmaharness` in Claude Code.
- **`agy` not found**: Doctor looks in PATH and in the standard install location `%LOCALAPPDATA%\agy\bin\agy.exe`. For a custom installation, set `agy_command` in the local TOML.
- **PowerShell cannot run scripts**: Check the source of the scripts and the execution policy of the machine. If allowed, you can run `powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\setup.ps1` for a single process, and run Doctor the same way.

### Using zMAHarness inside the Codex sandbox

Codex's Windows workspace-write sandbox redirects `%LOCALAPPDATA%` to `...\Packages\<package name>\AC\...`, while `%USERPROFILE%` stays the same. zMAHarness recognizes this redirect automatically and reads its settings and ledger from `%USERPROFILE%\AppData\Local` instead; Doctor also shows which path source is in use.

The sandbox only allows writes to the working folder, so dispatch must run outside the sandbox. When the pre-card `mah preflight` check detects the sandbox, the card's authorization limits note that dispatch needs one approval to run outside the sandbox, and Codex asks you to approve it once at dispatch time. If dispatch still runs inside the sandbox, `mah dispatch` stops with exit code 14 before creating a Run or consuming the Grant.

You can also set the environment variable `ZMAHARNESS_HOME` to choose the zMAHarness folder (replacing `%LOCALAPPDATA%\zMAHarness`); it must be an absolute path. Once set, `setup.ps1`, Doctor and `mah` all use only that location and never fall back to the old `MultiAgentHarness` path. `state` and `worktrees` are created under it by default, so it must not be inside any Git work tree (including the zMAHarness clone itself) or inside a cloud-synced folder (such as OneDrive); otherwise the CLI refuses to load the settings.

### Optional lightweight controller settings

```toml
[controller]
light_models = []
suggested_controller = "Codex Sol High"
```

`light_models` is a list of model ID strings and defaults to an empty list. Only when the controller confirms that its current model ID exactly matches one of the entries does it offer the lightweight-controller option on T1, T2, or T5 cards. `suggested_controller` is the name of the controller to suggest switching to, defaulting to `Codex Sol High`; you perform the switch in the controller UI.

## Roadmap

The following items are **planned and not yet implemented**.

1. OpenAI-compatible API worker (DeepSeek, and local models such as Ollama). Supports Context-Bundle only, and requires users to provide their own API key.

## Offline verification

Run the following commands from the root of the zMAHarness clone. The tests use synthetic data and local stand-ins.

```powershell
python -B -m unittest discover -s tests/v1 -q
python -B -m unittest discover -s tests/v2 -q
```

On Windows without Developer Mode, symlink-related tests normally show as skipped if the user cannot create symlinks. Environments with that permission still run the full assertions.

For V1 file handoff and interruption recovery rules, see the [role rules](docs/ROLES.md) and the [V1 inventory](docs/v2/V1_INVENTORY.md) (both in Chinese). To add a Worker Adapter, see [docs/ADAPTERS.md](docs/ADAPTERS.md). Keep local settings, SQLite, task records, and private verification reports on your machine. The Git whitelist only includes project code, general documentation, and de-identified test data.

## How this was built

The author defined the requirements, architecture, and acceptance criteria, and built the project in collaboration with multiple AI models:

- **Codex (GPT)**: main implementation and tests.
- **Claude**: part of the implementation, specification design, and independent acceptance review.
- **Gemini**: architecture review, and real review dispatches as a worker.

Every phase was merged only after independent review and test-based acceptance. The project itself practices zMAHarness's principles: the controller stays responsible, collaboration requires consent, and results are verified independently.

License: MIT, see [LICENSE](LICENSE). Please also see [DISCLAIMER.md](DISCLAIMER.md). To report a security issue, see [SECURITY.md](SECURITY.md).
