"""Rule engine: load a ruleset, execute checks, and expose rule-level state."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, List

import yaml

from app.core.config import get_settings
from app.schemas.ir import DocumentIR
from app.services.task_runtime import (
    TaskCancelled,
    TaskDeadlineExceeded,
    checkpoint,
    rule_scope,
)
from .base_checker import get_checker
from .rule_schema import RuleDef, RuleIssue, RuleSet

logger = logging.getLogger(__name__)


class RuleEngine:
    """Load a ruleset and run every rule against a DocumentIR."""

    def __init__(
        self,
        ruleset_path: str | Path,
        *,
        ruleset_text: str | None = None,
    ):
        self.ruleset_path = Path(ruleset_path)
        self.ruleset_text = ruleset_text
        self.ruleset = self._load_ruleset()

    def _load_ruleset(self) -> RuleSet:
        if self.ruleset_text is not None:
            return RuleSet(**yaml.safe_load(self.ruleset_text))
        if not self.ruleset_path.exists():
            raise FileNotFoundError(f"Ruleset not found: {self.ruleset_path}")
        with open(self.ruleset_path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        return RuleSet(**data)

    def run(
        self,
        ir: DocumentIR,
        on_rule_complete: Callable[[int, int, RuleDef], None] | None = None,
        on_rule_start: Callable[[int, int, RuleDef], None] | None = None,
        on_rule_result: Callable[[int, int, RuleDef, str, str], None] | None = None,
        should_skip: Callable[[RuleDef], bool] | None = None,
    ) -> List[RuleIssue]:
        all_issues: List[RuleIssue] = []
        enabled_rules = [rule for rule in self.ruleset.rules if rule.enabled]
        total_rules = len(enabled_rules)
        for index, rule in enumerate(enabled_rules, start=1):
            status = "done"
            error_message = ""
            try:
                checkpoint()
                if on_rule_start is not None:
                    on_rule_start(index, total_rules, rule)
                if should_skip is not None and should_skip(rule):
                    status = "skipped"
                    continue
                checker = get_checker(rule.type)
                with rule_scope(get_settings().RULE_TIMEOUT_SECONDS):
                    issues = checker.check(rule, ir)
                all_issues.extend(issues)
                if any(self._is_degraded(issue) for issue in issues):
                    status = "degraded"
                logger.debug(
                    "Rule %s (%s): %s issue(s), status=%s",
                    rule.id,
                    rule.type,
                    len(issues),
                    status,
                )
            except TaskCancelled:
                status = "cancelled"
                raise
            except TaskDeadlineExceeded:
                status = "timed_out"
                raise
            except Exception as exc:
                status = "failed"
                error_message = str(exc)
                logger.error(
                    "Rule %s (%s) crashed: %s",
                    rule.id,
                    rule.type,
                    exc,
                    exc_info=True,
                )
                all_issues.append(self._failed_issue(rule, error_message))
            finally:
                if on_rule_result is not None:
                    try:
                        on_rule_result(
                            index, total_rules, rule, status, error_message
                        )
                    except Exception:
                        logger.warning("Rule result callback failed", exc_info=True)
                if on_rule_complete is not None:
                    try:
                        on_rule_complete(index, total_rules, rule)
                    except Exception:
                        logger.warning("Rule progress callback failed", exc_info=True)
        return all_issues

    @staticmethod
    def _is_degraded(issue: RuleIssue) -> bool:
        return (
            issue.layer == "llm"
            and issue.severity == "info"
            and issue.message.startswith("AI 检查未执行")
        )

    @staticmethod
    def _failed_issue(rule: RuleDef, error_message: str) -> RuleIssue:
        return RuleIssue(
            rule_id=rule.id,
            severity="error",
            message=f"规则执行失败：{rule.description or rule.type}",
            suggestion="请检查规则配置或联系系统管理员后重新审查",
            evidence=error_message[:200],
            confidence="system",
            layer="system",
            checker=rule.type,
        )

    @property
    def summary(self) -> dict:
        return {
            "ruleset": self.ruleset.ruleset,
            "name": self.ruleset.name,
            "rule_count": len(self.ruleset.rules),
        }
