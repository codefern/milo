# Security Policy

Report vulnerabilities privately through GitHub Security Advisories for `codefern/milo`. Do not open a public issue containing an exploit, credential, private prompt, or sensitive log.

## Trust model

Milo is a local control plane around the official Codex, Claude Code, and Gemini CLIs. Provider authentication stays provider-owned. Milo checks authentication status but does not parse, copy, decrypt, or persist provider tokens.

Provider-native agents can execute tools according to the selected provider's own sandbox and approval configuration. Review that configuration before using Milo in a sensitive workspace. Milo's built-in tools apply an additional policy layer; this layer does not replace provider-native controls.

## Security boundaries

- File tools resolve canonical paths and are scoped to the active workspace.
- `/opt/va-backend` is an immutable protected root for Milo-managed writes.
- Terminal tools accept argument arrays only. Shell strings and shell executables are rejected.
- Git write operations require explicit approval; command aliases and execution overrides are rejected.
- Web research accepts public HTTPS targets only, resolves DNS, and rejects private, loopback, link-local, multicast, and reserved addresses.
- Remote MCP URLs pass the same public-network checks. MCP subprocesses receive a narrow environment allowlist rather than Milo's full environment.
- Skill manifests validate names, relative paths, versions, compatibility, file hashes, and declared installation data. Built-in catalog entries contain no automatic install commands.
- Context discovery ignores common secret files, private keys, virtual environments, caches, dependencies, and build output.
- Memory is project-scoped, bounded, searchable, and redacted by default.
- Milo state is created with private filesystem permissions. Session, memory, configuration, audit, automation, and checkpoint data remain local under `MILO_HOME`.
- Checkpoint restoration verifies recorded SHA-256 hashes before replacing workspace files.

## Operational guidance

1. Install provider CLIs only from their official distribution channels.
2. Keep provider CLIs and Milo dependencies current.
3. Use the least-privileged provider sandbox that can complete the task.
4. Inspect MCP and skill manifests before enabling third-party components.
5. Never place credentials directly in a task prompt or skill file.
6. Treat session databases, histories, checkpoints, and audit logs as sensitive local data.
7. Use `milo doctor`, `milo skills validate`, and the test suite after upgrades.
8. Remove stale state with the corresponding Milo commands rather than editing SQLite directly.

## Security verification

The repository's quality gate runs Ruff security rules, strict mypy checks, pytest security tests, package building, and dependency auditing. Tests cover path traversal, the protected root, shell-string rejection, Git alias execution, secret redaction, secret-file context exclusion, SSRF, unsafe MCP targets, skill traversal and integrity, private state permissions, checkpoint integrity, and audit-log redaction.

## Supported fixes

Security fixes target the latest release. Reports should include affected version, platform, provider, minimal reproduction, and impact without live credentials.
