# OpenWebUI Claude Code Pipe (fork)

Fork of [tfriedel/openwebui-claude-code](https://github.com/tfriedel/openwebui-claude-code).

Run [Claude Code](https://docs.claude.com/en/docs/claude-code/overview)'s agent loop from inside [Open WebUI](https://github.com/open-webui/open-webui) chats, via the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python).

> **Note:** This fork does not include the sandbox variant (`claude_agent_pipe_sandbox.py`) from the original repo — only the main pipe is maintained here.

## Changes from the original

### Bug fixes

- **Artifact upload** — `Files.insert_new_file()` is async; the original called it without `await`, so files were physically saved but never registered in the OpenWebUI database (links returned 404). Fixed with `async/await`.
- **`pipes()` method** — `return result` was inside the `for` loop, so only the first model was ever returned. Fixed.

### New features

- **Multi-model picker** — `MODEL` valve replaced by `MODELS` (format: `model_id:DisplayName`, comma-separated). Each entry appears as a separate model in the OpenWebUI model list — switch between Haiku and Sonnet with one click, no settings required.
- **Smart project folders** — workspace directories are named from the prompt (e.g. `Calc_python`) instead of raw `chat_id` UUIDs. The chat title in the sidebar is updated to match via the `chat:title` event.
- **`CLAUDE.md` template** — new `CLAUDE_MD_TEMPLATE` valve: point it at a file on disk and it gets copied into every new project directory before Claude starts. Zero tokens spent on generating project instructions.
- **Artifact links** — generated files (`.py`, `.js`, `.html`, images, etc.) are uploaded to OpenWebUI's file store and appear as download links at the end of each reply.
- **Thinking streamed live** — extended thinking tokens stream in real time inside `<thinking>` tags (OpenWebUI collapses them into a spoiler automatically), instead of buffering until the block ends.
- **Better tool display** — Write/Edit tool calls show the file content with proper syntax highlighting based on the file extension, instead of raw escaped JSON.

### Removed

- Fast-path routing (`/agent` / `/fast` prefixes and `_needs_agent` heuristic) — every turn runs the full agent loop.
- Sandbox pipe variant — not maintained in this fork.

---

## Features

- **Full Claude Code agent loop** — Read, Write, Edit, Bash, Glob, Grep, WebSearch, WebFetch (configurable allowlist)
- **Per-chat workspaces** — each chat gets a named working directory that persists across turns
- **Dual auth** — Anthropic **API key** (pay-per-token) *or* **Claude Pro/Max OAuth token** (subscription billing)
- **Streaming UI** — tool calls render inline with syntax-highlighted previews; generated files surface as download links
- **Configurable valves** — models, permission mode, tool allowlist, max turns, workspace root, CLAUDE.md template

## Requirements

- Open WebUI (any recent version with the Pipes/Functions framework)
- Python deps (auto-installed by Open WebUI from the file header):
  - `claude-agent-sdk>=0.1.60`
  - `anthropic>=0.40.0`
- The `claude` CLI must be available on the host running Open WebUI's Python backend (the SDK shells out to it). Install via `npm install -g @anthropic-ai/claude-code`.

## Installation

1. In Open WebUI, go to **Workspace → Functions → +** (or **Admin Panel → Functions**).
2. Paste the contents of [`claude_code.py`](./claude_code.py) into the editor.
3. Save and enable the function.
4. Open the function's **Valves** and configure auth (one of):
   - `ANTHROPIC_API_KEY` — standard pay-per-token billing
   - `CLAUDE_CODE_OAUTH_TOKEN` — generate on a machine with a browser via `claude setup-token`; bills against your Pro/Max/Team subscription
5. Two models — **Claude Code (Haiku)** and **Claude Code (Sonnet)** — will appear in the model picker. Customize via the `MODELS` valve.

## Configuration (Valves)

| Valve | Default | Description |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | *(env)* | Anthropic API key. Falls back to the backend's env var. |
| `CLAUDE_CODE_OAUTH_TOKEN` | *(empty)* | Claude subscription OAuth token. Takes priority over the API key when set. |
| `MODELS` | `claude-haiku-4-5:Haiku,claude-sonnet-4-6:Sonnet` | Comma-separated list of models in `model_id:DisplayName` format. Each entry appears as a separate model in the picker. |
| `PERMISSION_MODE` | `bypassPermissions` | `default`, `acceptEdits`, `bypassPermissions`, `plan`, or `dontAsk`. |
| `ALLOWED_TOOLS` | `Read,Write,Edit,Bash,Glob,Grep,WebSearch,WebFetch` | Comma-separated tools auto-approved without prompting. |
| `WORKDIR_ROOT` | `/tmp/claude-agent-pipe` | Root directory for per-chat workspaces. Subdirectories are named from the prompt. |
| `MAX_TURNS` | `30` | Max agent turns per user message. `0` disables the cap. |
| `CLAUDE_MD_TEMPLATE` | *(empty)* | Path to a `CLAUDE.md` file to copy into each new project directory. Example: `/home/user/claude-template/CLAUDE.md`. |

## Auth notes

When both auth methods are present, the OAuth token wins and the API key is unset before invoking the SDK so it can't override.

Per Anthropic's terms: a Claude subscription is for personal use — **don't re-offer subscription auth to other end users** through a shared Open WebUI deployment. For multi-user setups, use API keys.

## License

MIT