# Hermes capability comparison

This checklist was researched against the public Hermes Agent documentation and repository, then mapped to executable Milo tests. Milo does not copy Hermes source.

| Capability | Hermes public behavior | Milo implementation | Improvement verified in Milo |
|---|---|---|---|
| Setup | Interactive provider/model setup and diagnostics | Three-provider wizard with installation/auth table and curated skills | Existing native sessions are reused; credential values never enter Milo; setup is scriptable and validates installed skills |
| Providers | Broad API/OAuth provider matrix | Exactly Codex, Claude Code, and Gemini CLI | Native CLI behavior stays provider-specific instead of being flattened behind an API approximation |
| Agent loop | Tool-calling model loop | Official provider loop wrapped by durable control plane | Provider upgrades deliver new tools without a Milo rewrite |
| Delegation | `delegate_task` and independent agents | Automatic deterministic task gate, dynamic roles, bounded pool, failure-tolerant synthesis | Users do not have to decide when to delegate; simple tasks prove zero sub-agent overhead |
| Tools | Large built-in tool registry with schemas | Small modular registry for terminal/files/Git/GitHub/research/code | One permission per tool, argv-only execution, canonical workspace boundary, public-network research checks |
| Skills | Discoverable procedural skill packages | Machine-readable 18-skill catalog and 15 curated recommendations | Atomic installs, source trust, compatibility checks, symlink/traversal rejection, per-file SHA-256 verification |
| Memory | Persistent user and agent notes | Project-aware bounded SQLite FTS5 store | Memory is directly inspectable, searchable, editable, removable, and separate from conversation history |
| Context | Project instruction files and compression | Ranked relevant files with progressive disclosure and a hard budget | Dependency trees, caches, binaries, databases, build output, and symlinks are excluded before ranking |
| Sessions | SQLite/FTS session store and resume commands | Milo session metadata plus provider-native continuation | One stable Milo identifier survives provider differences and search is project-scoped |
| Recovery | Session continuity and Git/worktree workflows | Hashed selected-file checkpoints plus Git | Recovery works before a Git commit and detects checkpoint corruption |
| MCP | Native stdio and HTTP discovery with filtered environment | Validated stdio/HTTPS definitions delegated to provider-native MCP | Shell interpreters and insecure URLs are rejected at configuration time; automation remains provider sandboxed |
| Research | Search, extraction, browser, and citations | Provider web/browser tools plus built-in public HTTPS retrieval and source synthesis | Built-in fetch blocks local/private-network destinations and enforces content/size limits |
| Automation | Cron, webhooks, background jobs | Private schedule registry | Persistent jobs are always created paused and require a distinct enable action |
| Security | Redaction and configurable approvals | Recursive redaction, strict state modes, protected roots, argv policy, skill hashes | Security boundaries are deterministic and testable without an auxiliary model approval call |
| UX | CLI/TUI/desktop/gateway surfaces | Focused CLI with Rich tables, concise setup, status, and management commands | Lower startup/storage footprint and less surface complexity for terminal-first provider users |
| Resource use | Full Python agent plus optional surfaces/plugins | Thin Python harness around installed providers | No model weights, no resident daemon, no silent background workers, bounded agent count |

## Public sources

- Hermes Agent documentation: https://hermes-agent.nousresearch.com/docs/
- Hermes Agent repository: https://github.com/NousResearch/hermes-agent
- Hermes providers: https://hermes-agent.nousresearch.com/docs/integrations/providers
- Hermes native MCP: https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp
- OpenAI Codex CLI repository/docs: https://github.com/openai/codex
- Anthropic Claude Code CLI reference: https://code.claude.com/docs/en/cli-reference
- Google Gemini CLI documentation: https://geminicli.com/docs/
- Google Gemini CLI repository: https://github.com/google-gemini/gemini-cli

## Verification gates

- Unit/integration tests assert provider-specific argv, stream parsing, delegation, persistence, context, checkpoint, tool, MCP, skill, and security behavior.
- Real Codex execution confirms setup, response streaming, and session creation.
- Real provider diagnostics distinguish installation, configured auth, and runtime cloud credential failures.
- An isolated tmux run exercises simple/complex tasks, tool use, continuation, Git/GitHub, skills, and recovery.
- `ruff`, strict `mypy`, wheel build, package install, dependency audit, and secret scan run before release.
