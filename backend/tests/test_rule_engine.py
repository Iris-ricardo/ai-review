from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.schemas.ir import DocumentIR
from app.services.rules.base_checker import BaseChecker, register_checker
from app.services.rules.engine import RuleEngine
from app.services.rules.rule_schema import RuleDef


@register_checker("crashing_test_checker")
class CrashingTestChecker(BaseChecker):
    checker_name = "crashing_test_checker"

    def check(self, rule: RuleDef, ir: DocumentIR):
        raise RuntimeError("boom")


def test_engine_promotes_checker_exception_to_blocking_system_issue(tmp_path):
    ruleset_path = tmp_path / "crashing.yaml"
    ruleset_path.write_text(
        "\n".join([
            "ruleset: crashing_test_v1",
            "name: crashing",
            "rules:",
            "  - id: T001",
            "    type: crashing_test_checker",
            "    severity: error",
        ]),
        encoding="utf-8",
    )

    issues = RuleEngine(ruleset_path).run(DocumentIR())

    assert len(issues) == 1
    assert issues[0].rule_id == "T001"
    assert issues[0].severity == "error"
    assert issues[0].checker == "crashing_test_checker"
    assert issues[0].confidence == "system"
    assert issues[0].layer == "system"
    assert issues[0].message.startswith("规则执行失败")


def test_engine_reports_rule_progress(tmp_path):
    ruleset_path = tmp_path / "progress.yaml"
    ruleset_path.write_text(
        "\n".join([
            "ruleset: progress_v1",
            "name: progress",
            "rules:",
            "  - id: P001",
            "    type: crashing_test_checker",
            "    severity: error",
            "  - id: P002",
            "    type: crashing_test_checker",
            "    severity: error",
        ]),
        encoding="utf-8",
    )
    progress = []

    RuleEngine(ruleset_path).run(
        DocumentIR(),
        on_rule_complete=lambda completed, total, rule: progress.append(
            (completed, total, rule.id)
        ),
    )

    assert progress == [(1, 2, "P001"), (2, 2, "P002")]
