# Architecture

Milo is a small control plane around official provider CLIs. The provider remains the model runtime and tool executor; Milo owns setup, policy, durable state, delegation decisions, skills, and recovery.

## Boundaries

- `cli.py`: setup, diagnostics, interactive input, and management commands.
- `agent.py`: session lifecycle, provider invocation, automatic delegation, failure collection, and synthesis.
- `providers/`: executable/auth detection, official argv construction, streaming JSONL parsing, model flags, and native resume identifiers.
- `orchestrator.py`: deterministic complexity classification and dynamic role selection. Concurrency is bounded by configuration.
- `tools.py`: schema validation, permissions, audit events, and built-in terminal/files/Git/GitHub/research/code tools.
- `skills.py`: catalog validation and atomic install/update/remove.
- `memory.py`, `sessions.py`, `storage.py`: private SQLite persistence, FTS5 search, retention bounds, and foreign-key integrity.
- `context.py`: relevant-file discovery and progressive disclosure.
- `checkpoints.py`: selected-file snapshots with integrity hashes and workspace boundaries.
- `mcp.py`: secure, inspectable command/HTTPS server definitions and filtered environments.
- `automation.py`: paused-by-default schedule registry.
- `security.py`: path, command, redaction, and risk primitives.

## Agent lifecycle

1. Load validated configuration.
2. Open or create the Milo session.
3. Classify task complexity.
4. For simple tasks, invoke one native provider process.
5. For complex tasks, choose only relevant roles, run them in a bounded thread pool, collect failures without discarding successful evidence, and ask the lead provider session to reconcile results.
6. Persist the user request, response, native session identifier, delegation decision, and roles.
7. Resume through the provider's official continuation command.

## Context and storage

Project paths are canonicalized before access. Context scanning excludes dependency trees, caches, binary formats, state databases, symlinks, and build output. Selection favors explicit paths, then keyword frequency, and truncates around relevant text.

SQLite runs with foreign keys and WAL. State directories are `0700`; database and JSON state are `0600`. Sessions and memory are separate because conversational records and durable facts have different retention and editing semantics.

## Failure model

Provider absence, auth failure, malformed streams, and non-zero exits become concise domain errors. Sub-agent failures are recorded and successful work continues; all-agent failure aborts synthesis. Skill updates stage and validate before atomic replacement. Checkpoint restores validate stored SHA-256 hashes before overwriting workspace files.
