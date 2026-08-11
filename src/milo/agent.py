from __future__ import annotations

import concurrent.futures
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .orchestrator import DelegationDecision, Orchestrator
from .providers import InvocationError, get_provider
from .sessions import SessionStore


@dataclass(frozen=True)
class AgentResult:
    text: str
    session_id: str
    provider_session_id: str | None
    delegation: DelegationDecision


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key in ("text", "result", "output_text"):
            text = value.get(key)
            if isinstance(text, str) and text.strip():
                yield text
        for key in ("item", "message", "content", "delta", "response"):
            if key in value:
                yield from _strings(value[key])
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _provider_session(event: dict[str, object]) -> str | None:
    for key in ("session_id", "thread_id"):
        value = event.get(key)
        if isinstance(value, str):
            return value
    item = event.get("item")
    return _provider_session(item) if isinstance(item, dict) else None


def _stream_text(event: dict[str, object]) -> str | None:
    if event.get("type") == "item.completed":
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            return text if isinstance(text, str) else None
    if event.get("type") == "stream_event":
        stream_event = event.get("event")
        if isinstance(stream_event, dict):
            delta = stream_event.get("delta")
            if isinstance(delta, dict) and isinstance(delta.get("text"), str):
                return str(delta["text"])
    if event.get("type") == "message" and event.get("role") == "assistant":
        content = event.get("content")
        return content if isinstance(content, str) else None
    return None


def _retryable_provider_error(error: Exception) -> bool:
    message = str(error).casefold()
    return any(
        signal in message
        for signal in (
            "timeout",
            "timed out",
            "temporarily",
            "rate limit",
            "unavailable",
            "connection reset",
        )
    )


class Agent:
    def __init__(
        self,
        config: Config,
        state_db: str | Path,
        *,
        project: str,
        provider_factory: Callable[[str], Any] = get_provider,
        on_text: Callable[[str], object] | None = None,
        on_delegation: Callable[[DelegationDecision], object] | None = None,
    ) -> None:
        self.config = config
        self.state_db = Path(state_db)
        self.project = project
        self.provider_factory = provider_factory
        self.on_text = on_text
        self.on_delegation = on_delegation

    def _invoke(
        self, prompt: str, *, provider_session_id: str | None = None, emit: bool = True
    ) -> tuple[str, str | None]:
        provider = self.provider_factory(self.config.provider)
        if not provider.detect():
            raise InvocationError(
                f"{self.config.provider} CLI is not installed. Run `milo setup` for official installation guidance."
            )
        chunks: list[str] = []
        remote_id = provider_session_id
        for event in provider.stream(
            prompt, model=self.config.model, session_id=provider_session_id
        ):
            remote_id = _provider_session(event) or remote_id
            chunks.extend(_strings(event))
            streamed = _stream_text(event)
            if emit and streamed and self.on_text:
                self.on_text(streamed)
        text = "\n".join(dict.fromkeys(part.strip() for part in chunks if part.strip()))
        return text or "Provider completed without a textual response.", remote_id

    def run(
        self, task: str, *, resume: str | None = None, delegate: bool | None = None
    ) -> AgentResult:
        with SessionStore(self.state_db) as sessions:
            session = sessions.get(resume) if resume else None
            if resume and session is None:
                raise KeyError(f"session not found: {resume}")
            if session is None:
                session = sessions.create(self.project, {"provider": self.config.provider})
            sessions.add_message(session.id, "user", task)
            remote_id = next(
                (
                    message.metadata.get("provider_session_id")
                    for message in reversed(session.messages)
                    if message.metadata.get("provider_session_id")
                ),
                None,
            )
            decision = Orchestrator.decide(task, max_agents=self.config.max_agents)
            if delegate is False:
                decision = DelegationDecision(False, (), "delegation disabled")
            elif delegate is True and not decision.delegate:
                decision = DelegationDecision(
                    True, ("implementer", "reviewer"), "delegation requested"
                )

            if not decision.delegate:
                text, remote_id = self._invoke(task, provider_session_id=remote_id)
            else:
                if self.on_delegation:
                    self.on_delegation(decision)

                def run_role(role: str) -> tuple[str, str]:
                    prompt = (
                        f"You are Milo's {role}. Work independently on the task below. "
                        "Use least privilege, verify claims, and return a concise result for the lead agent.\n\n"
                        f"{task}"
                    )
                    for attempt in range(2):
                        try:
                            result, _ = self._invoke(prompt, emit=False)
                            return role, result
                        except InvocationError as exc:
                            if attempt == 0 and _retryable_provider_error(exc):
                                continue
                            raise
                    raise InvocationError(f"{role} exhausted its retry budget")

                results: dict[str, str] = {}
                failures: list[str] = []
                with concurrent.futures.ThreadPoolExecutor(max_workers=len(decision.roles)) as pool:
                    futures = {pool.submit(run_role, role): role for role in decision.roles}
                    for future in concurrent.futures.as_completed(futures):
                        role = futures[future]
                        try:
                            key, value = future.result()
                            results[key] = value
                        except Exception as exc:
                            failures.append(f"{role}: {exc}")
                if not results:
                    raise InvocationError("all sub-agents failed: " + "; ".join(failures))
                synthesis = Orchestrator.synthesis_prompt(task, results)
                if failures:
                    synthesis += (
                        "\n\nFailed agents (continue using successful evidence): "
                        + "; ".join(failures)
                    )
                text, remote_id = self._invoke(synthesis, provider_session_id=remote_id)

            sessions.add_message(
                session.id,
                "assistant",
                text,
                {
                    "provider_session_id": remote_id,
                    "delegated": decision.delegate,
                    "roles": list(decision.roles),
                },
            )
            return AgentResult(text, session.id, remote_id, decision)
