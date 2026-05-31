# DevMem

**Local-first developer memory layer.** Captures every developer activity — terminal commands, git commits, file edits, repo opens, AI chat sessions — into a structured SQLite database with a FAISS vector index. Enables natural language recall:

```
devmem ask "what did I work on last Tuesday?"
devmem ask "how did I fix the auth bug?"
devmem today
devmem timeline
```

---

## Quick Start

```bash
pip install devmem
devmem init
```

`devmem init` will:
1. Create `~/.local/share/devmem/` and `~/.config/devmem/`
2. Initialize the SQLite database + FAISS vector index
3. Install the zsh/bash shell hook (appends `source` line to your rc file)
4. Set `git config --global core.hooksPath` to capture all commits
5. Start the background daemon (via systemd user service or subprocess)

---

## Commands

| Command | Description |
|---------|-------------|
| `devmem init` | First-time setup |
| `devmem ask "<query>"` | Natural language search with LLM answer |
| `devmem today` | Summary of today's activity |
| `devmem week` | Summary of this week's activity |
| `devmem timeline` | Chronological event list for a day |
| `devmem search "<query>"` | Raw hybrid search (no LLM) |
| `devmem repos` | List all tracked repos |
| `devmem stats` | Capture statistics |
| `devmem export` | Export events as JSON or CSV |
| `devmem config` | View/edit configuration |
| `devmem privacy list` | Show what's captured |
| `devmem privacy delete` | Delete captured events |
| `devmem privacy ignore --cmd "pattern"` | Add a privacy filter |
| `devmem daemon start/stop/status/logs` | Manage the background daemon |
| `devmem mcp-serve` | Start MCP server (for Claude Code / Copilot) |

---

## Architecture

```
Collectors (shell hook, git hooks, VS Code ext, AI log watcher)
    ↓ events (TSV files + HTTP POST)
Daemon (FileWatcher → Enricher → SessionDetector → DB insert → Embedder)
    ↓
Storage (SQLite events.db + FTS5, FAISS vectors.faiss)
    ↓
Query (Hybrid FAISS+FTS5 → RRF → LLM via OpenRouter)
    ↓
CLI + MCP Server
```

---

## Data Sources

### Shell commands (zsh / bash)
Add to `~/.zshrc` (done automatically by `devmem init`):
```bash
source ~/.config/devmem/hook.zsh
```

### Git commits
`devmem init` sets `core.hooksPath` globally — all future commits in any repo are captured.

### VS Code activity
Install the extension:
```bash
code --install-extension devmem.devmem-vscode
```

### AI chat sessions
Automatically scanned from:
- **GitHub Copilot Chat**: `~/.config/Code/User/workspaceStorage/*/GitHub.copilot-chat/debug-logs/`
- **Claude Code**: `~/.claude/projects/*/sessions/`
- **Aider**: `.aider.chat.history.md` in git repos
- **Cursor**: `~/.config/Cursor/User/workspaceStorage/`

---

## LLM Integration

DevMem uses [OpenRouter](https://openrouter.ai) for the `ask` command and daily summaries.

```bash
export OPENROUTER_API_KEY=sk-or-...
devmem ask "what was I debugging yesterday?"
```

Without an API key, `devmem ask` falls back to `--no-llm` mode (shows retrieved events directly). All other commands work fully offline.

---

## MCP Server

Use DevMem as a context source in Claude Code or VS Code Copilot:

**Claude Code** (`~/.config/claude/mcp.json`):
```json
{
  "mcpServers": {
    "devmem": {
      "command": "devmem",
      "args": ["mcp-serve"]
    }
  }
}
```

**VS Code / Copilot** (`.vscode/mcp.json`):
```json
{
  "servers": {
    "devmem": {
      "type": "stdio",
      "command": "devmem",
      "args": ["mcp-serve"]
    }
  }
}
```

Available MCP tools: `recall`, `today_summary`, `recent_repos`, `find_command`, `timeline`

---

## Privacy

- Commands matching `*password*`, `*secret*`, `*token*` etc. are **dropped before storage**
- AI chat messages are truncated to 200 characters (intent only, not full content)
- File saves store only **path + language**, never file content
- All data is **local only** — LLM calls send only small event snippets, only when you run `ask`
- Default retention: **90 days** (configurable)

```bash
devmem privacy list           # see what's captured
devmem privacy delete --before 2026-01-01
devmem privacy ignore --cmd "*mycompany*"
```

---

## Configuration

Config file: `~/.config/devmem/config.json`

```bash
devmem config                          # show all settings
devmem config daemon_port 8080        # change port
devmem config retention_days 30       # shorter retention
```

Key settings:
```json
{
  "daemon_port": 27182,
  "embedding_model": "all-MiniLM-L6-v2",
  "llm_model": "anthropic/claude-sonnet-4",
  "retention_days": 90,
  "capture": { "terminal": true, "git": true, "vscode": true, "ai_chat": true }
}
```

---

## Data Storage

```
~/.local/share/devmem/
├── events.db       # SQLite (events + FTS5 + sessions + daily_summaries)
├── vectors.faiss   # FAISS vector index
├── shell.tsv       # shell hook ring buffer
├── git.tsv         # git hook ring buffer
└── daemon.pid      # running daemon PID

~/.config/devmem/
├── config.json
├── hook.zsh / hook.bash
└── git-hooks/post-commit + post-checkout
```

---

## Development

```bash
git clone <repo>
cd devmem
pip install -e ".[dev]"
pytest
```

### Sandbox testing

`devmem init` makes system-wide changes (modifies `~/.zshrc`, sets a global `git config core.hooksPath`, starts a background daemon). Use the provided Docker sandbox to test safely without touching your host environment.

**Prerequisites:** Docker

```bash
# Interactive shell — explore freely
./sandbox.sh

# Run the full test suite
./sandbox.sh test

# Run `devmem init` and inspect every file it creates
./sandbox.sh init

# Run any arbitrary command
./sandbox.sh "devmem --help"
```

What the sandbox isolates:

| Risk | Mitigation |
|------|------------|
| Modifies `~/.zshrc` | Only affects the container's home directory |
| Sets global `git config core.hooksPath` | Sandboxed git config, discarded on exit |
| Starts a background daemon | Killed automatically when the container exits |
| Network calls to OpenRouter | Blocked via `--network none` |
| Privilege escalation | `--cap-drop ALL --security-opt no-new-privileges` |

Alternatively, use a throwaway VM: `multipass launch --name devmem-test`

---

## Roadmap

- **v0.1** (current): Shell + git + daemon + CLI ask/today/timeline/stats
- **v0.2**: VS Code extension + AI chat parsers + week/repos/search/export + auto-summary
- **v0.3**: MCP server + privacy management + Aider/Cursor parsers
- **v1.0**: Cross-machine sync + web dashboard + Wakatime-compatible API

---

## License

MIT
