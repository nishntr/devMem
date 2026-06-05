## [0.2.0] - 2026-06-06

### Added
- Timestamp-based duplicate detection in AIChatCollector to prevent false positives across sessions

### Changed
- Refactored event processing pipeline for improved maintainability
- Reorganized module structure across 49 files
- Updated API interfaces across collectors
- Improved logging, debugging output, and configuration validation

### Fixed
- Duplicate AI chat session detection
- Session boundary detection edge cases
- Error handling in background daemon
- Minor bugs in event enrichment

### Improved
- Code organization, readability, and type hints
- Test coverage
- Documentation accuracy

## [0.1.0] - 2026-05-31

### Added
- Initial release of Recall: Local-first developer memory layer
- Shell command capture via zsh/bash hooks with ring buffer storage
- Git commit tracking via global git hooks
- Multi-source event collection:
  - Shell commands from terminal
  - Git commits from all repositories
  - AI chat sessions (GitHub Copilot Chat, Claude Code, Aider, Cursor)
  - VS Code activity preparation (framework in place)
- Daemon service with FileWatcher, Enricher, SessionDetector, and Embedder pipeline
- SQLite database with FTS5 full-text search capabilities
- FAISS vector index for semantic search
- Hybrid search combining FTS5 and semantic vectors with RRF ranking
- LLM integration via OpenRouter for natural language queries
- CLI tools: `ask`, `today`, `week`, `timeline`, `search`, `repos`, `stats`, `export`, `config`
- Privacy management with sensitive data filtering and retention policies
- MCP server support for Claude Code and VS Code Copilot integration
- Background daemon management (start/stop/status/logs)
- Configuration system with customizable settings
- Docker sandbox for safe development and testing
- Comprehensive documentation and README

### Changed
- Initial project structure and architecture

### Fixed
- N/A (Initial release)

### Improved
- N/A (Initial release)