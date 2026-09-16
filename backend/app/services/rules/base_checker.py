"""Base class for all deterministic rule checkers.

Each checker examines the DocumentIR and returns RuleIssue objects, and registers
itself via a type name so the rule engine can dispatch by ``rule.type``."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Dict, List, Type

from app.schemas.ir import DocumentIR
from .rule_schema import RuleDef, RuleIssue

# ── Global registry ───────────────────────────────────────
_registry: Dict[str, Type["BaseChecker"]] = {}


def register_checker(type_name: str):
    """Class decorator to register a checker under a given type name."""
    def decorator(cls: Type["BaseChecker"]):
        _registry[type_name] = cls
        return cls
    return decorator


def get_checker(type_name: str) -> "BaseChecker":
    """Look up a checker class by type name."""
    cls = _registry.get(type_name)
    if cls is None:
        raise ValueError(
            f"No checker registered for type '{type_name}'. "
            f"Available: {list(_registry.keys())}"
        )
    return cls()


def list_registered_checker_types() -> List[str]:
    """Return all registered checker type names."""
    return sorted(_registry.keys())


class BaseChecker(ABC):
    """Abstract base for a single rule check."""

    # Override in subclasses
    checker_name: ClassVar[str] = "base"

    @abstractmethod
    def check(self, rule: RuleDef, ir: DocumentIR) -> List[RuleIssue]:
        """Run ``rule`` (YAML rule def: id, severity, params) against the unified IR ``ir``.

        Returns the issues found, empty if all checks pass."""
        ...

    def _issue(
        self,
        rule: RuleDef,
        message: str,
        page: int | None = None,
        block_id: str | None = None,
        evidence: str = "",
        suggestion: str = "",
    ) -> RuleIssue:
        """Convenience factory for creating a RuleIssue."""
        return RuleIssue(
            rule_id=rule.id,
            severity=rule.severity,
            page=page,
            block_id=block_id,
            evidence=evidence[:200],
            message=message,
            suggestion=suggestion,
            confidence="deterministic",
            layer="rule",
            checker=self.checker_name,
        )
