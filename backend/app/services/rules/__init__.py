"""Rule engine and all deterministic checkers.

Importing this package registers all checker classes via @register_checker.
"""
from .base_checker import BaseChecker, get_checker, register_checker
from .engine import RuleEngine
from .rule_schema import RuleDef, RuleIssue, RuleSet

# Trigger checker registration
from . import checkers_structure   # noqa: F401
from . import checkers_format      # noqa: F401
from . import checkers_content     # noqa: F401
from . import checkers_meta        # noqa: F401
from . import checkers_fields      # noqa: F401
from . import checkers_layout      # noqa: F401
from app.services.llm import checkers as llm_checkers  # noqa: F401

__all__ = [
    "BaseChecker", "get_checker", "register_checker",
    "RuleEngine", "RuleDef", "RuleIssue", "RuleSet",
]
