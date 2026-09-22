# Smart Things Planner

Turn Gmail and natural-language requests into reviewed, workload-aware Things 3 tasks. The same skill works in Codex and Claude Code.

## What it does

- Scans one or more Gmail accounts through a read-only local MCP server.
- Reads message bodies instead of deciding from subject lines alone.
- Suggests start dates from your existing Things workload over a default 14-day window.
- Discovers your current Things Areas dynamically instead of hardcoding names.
- Uses exactly one workload tag per task: `🍅`, `🍅🍅`, `🍅🍅🍅`, `☀️`, or `☀️☀️`.
- Treats any dedicated trip away from home as at least `☀️`.
- Shows a fixed preview table and requires approval before creating anything.

The Things bridge can list structure and open workload, find mail source markers, initialize the five workload tags with confirmation, and create approved tasks. It has no tools to complete, delete, cancel, or edit existing tasks. The Gmail bridge has no send, draft, label, archive, trash, or delete tools.

## Requirements

- macOS with [Things 3](https://culturedcode.com/things/)
- Python 3.10+
- Codex, Claude Code, or both
- [Google Workspace CLI (`gws`)](https://github.com/googleworkspace/cli) for Gmail scanning

Install `gws` with Homebrew:

```bash
brew install googleworkspace-cli
```

## Install

Clone the repository, then run the installer from its root. The installer can launch any Python 3 available on your Mac and will automatically choose Python 3.10+ for its private environment. Gmail accounts are optional and can be repeated:

```bash
python3 scripts/install.py \
  --gmail-profile personal=~/.config/gws-personal \
  --gmail-profile school=~/.config/gws-school
```

Use `--client codex` or `--client claude` to install for only one client. The installer creates a local virtual environment, links the shared skill into each client, and registers separate MCP processes for Things and each Gmail account. Existing matching entries are preserved; pass `--force` only when you intentionally want to replace them.

Restart Codex or Claude Code after installation.

## Authorize Gmail

Each Gmail account uses an isolated configuration directory. You can either run `gws auth setup` for each profile or create a Google Cloud Desktop OAuth client yourself, enable the Gmail API, and place its downloaded JSON at `client_secret.json` inside each profile directory.

```bash
GOOGLE_WORKSPACE_CLI_CONFIG_DIR=~/.config/gws-personal gws auth setup
GOOGLE_WORKSPACE_CLI_CONFIG_DIR=~/.config/gws-school gws auth setup
```

Then authorize each account read-only:

```bash
GOOGLE_WORKSPACE_CLI_CONFIG_DIR=~/.config/gws-personal gws auth login --readonly -s gmail
GOOGLE_WORKSPACE_CLI_CONFIG_DIR=~/.config/gws-school gws auth login --readonly -s gmail
```

Never commit client-secret JSON or OAuth tokens. The included `.gitignore` blocks common credential filenames.

## First use

Ask the agent to initialize the workload tags. This operation still requires an explicit confirmation:

> Initialize the Things workload tags for Smart Things Planner.

Then try either mode:

> Check both Gmail accounts for the last 14 days, preview candidate tasks, and do not write to Things yet.

> Schedule three harmonica practice sessions over the next week.

Every candidate preview uses:

`# | 标题 | 去向 | 负荷 | 开始日期 | 截止日期 | 依据与说明`

Missing deadlines are always shown as `N/A`. Approval applies only to the displayed version.

## Privacy and performance

- Full Gmail messages are prefetched with eight workers and cached only in the MCP process memory.
- Each account has its own cache, limited to 512 messages.
- Email bodies are cleaned before being returned to the agent and are never written to disk by this project.
- The cache disappears when the MCP process exits.
- Things is controlled through AppleScript/JXA and Things URL schemes; the database and Things Cloud credentials are never accessed.

## Development

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

The repository contains both Codex and Claude Code plugin metadata. The installer remains the recommended setup because it creates an isolated Python environment and supports multiple Gmail accounts.

## License

[MIT](LICENSE)
