from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class DelegationDecision:
    delegate: bool
    roles: tuple[str, ...]
    reason: str


class Orchestrator:
    """A deterministic, inspectable delegation gate before provider invocation."""

    _complex = re.compile(
        r"\b(research|implement|architecture|security|review|tests?|debug|migrate|multiple|parallel|end[- ]to[- ]end)\b",
        re.I,
    )

    @classmethod
    def decide(cls, task: str, *, max_agents: int = 3) -> DelegationDecision:
        max_agents = max(1, max_agents)
        signals = {
            match.group(1).lower().removesuffix("s") for match in cls._complex.finditer(task)
        }
        multi_clause = sum(task.lower().count(word) for word in (" and ", ",", ";")) >= 2
        if len(signals) < 2 and not (signals and multi_clause) and len(task.split()) < 35:
            return DelegationDecision(False, (), "simple task")
        roles: list[str] = []
        if "research" in signals:
            roles.append("researcher")
        if signals & {"implement", "architecture", "migrate", "debug"}:
            roles.append("implementer")
        if signals & {"test", "end-to-end"}:
            roles.append("tester")
        if signals & {"security", "review"}:
            roles.append("security-reviewer" if "security" in signals else "reviewer")
        if len(roles) < 2:
            roles.extend(["implementer", "reviewer"])
        unique = tuple(dict.fromkeys(roles))[:max_agents]
        return DelegationDecision(True, unique, f"{len(signals)} complexity signals")

    @staticmethod
    def synthesis_prompt(task: str, results: dict[str, str]) -> str:
        sections = "\n\n".join(f"## {role}\n{value}" for role, value in results.items())
        return f"Synthesize and validate these independent results for the original task. Resolve conflicts explicitly.\n\nTask: {task}\n\n{sections}"
