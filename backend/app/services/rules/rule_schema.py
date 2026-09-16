"""
Pydantic schemas for rule definitions and rule engine output.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class RuleParam(BaseModel):
    """A single parameter definition inside a rule."""
    name: str
    value: Any


class RuleDef(BaseModel):
    """One rule definition as parsed from YAML."""
    id: str
    type: str
    severity: str = "error"  # error | warning | info
    description: str = ""
    params: Dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    basis: str | Dict[str, Any] = ""
    tags: List[str] = Field(default_factory=list)
    review_note: str = ""

    @field_validator("id", "type")
    @classmethod
    def validate_required_identifier(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("rule id and type cannot be empty")
        return value

    @field_validator("id")
    @classmethod
    def validate_rule_id_format(cls, value: str) -> str:
        import re

        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{1,31}", value) is None:
            raise ValueError("rule id must start with a letter and contain 2-32 letters, digits, _ or -")
        return value

    @field_validator("severity")
    @classmethod
    def validate_severity(cls, value: str) -> str:
        if value not in {"error", "warning", "info"}:
            raise ValueError("severity must be error, warning or info")
        return value


class RuleSet(BaseModel):
    """A complete ruleset loaded from a YAML file."""
    ruleset: str
    name: str = ""
    description: str = ""
    rules: List[RuleDef] = Field(default_factory=list)

    @field_validator("ruleset")
    @classmethod
    def validate_ruleset_id(cls, value: str) -> str:
        import re

        value = value.strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) is None:
            raise ValueError("ruleset id may contain only letters, digits, _ or -")
        return value


class RuleIssue(BaseModel):
    """A single issue found by a rule checker."""
    rule_id: str
    severity: str  # error | warning | info
    page: Optional[int] = None
    block_id: Optional[str] = None
    bbox: Optional[List[float]] = None  # [x0, y0, x1, y1] for PDF highlight
    evidence: str = ""
    message: str = ""
    suggestion: str = ""
    confidence: str = "deterministic"
    layer: str = "rule"
    checker: str = ""
