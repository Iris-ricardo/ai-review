"""B4: 业务结论决策表（六）— 任务状态与业务结论严格分离的回归测试。

inputs 对应 _execute_review 统计量：no_executable_rules（空规则集/全部禁用/遗留快照）、incomplete_rules
（failed + degraded + skipped）、manual_required、errors。期望：incomplete > needs_revision > pass，未完成有效审查不得判 pass。"""
from __future__ import annotations

import itertools

import pytest

from app.api.routes import conclude_review


def _expect(no_exec, incomplete, manual, errors) -> str:
    if no_exec or incomplete or manual:
        return "incomplete"
    if errors:
        return "needs_revision"
    return "pass"


@pytest.mark.parametrize(
    "no_exec,incomplete,manual,errors",
    list(itertools.product([False, True], [0, 1], [0, 1], [0, 1])),
)
def test_conclusion_decision_table(no_exec, incomplete, manual, errors):
    expected = _expect(no_exec, incomplete, manual, errors)
    actual = conclude_review(
        no_executable_rules=no_exec,
        incomplete_rules=incomplete,
        manual_required=manual,
        errors=errors,
    )
    assert actual == expected


def test_empty_ruleset_never_passes():
    assert conclude_review(
        no_executable_rules=True, incomplete_rules=0, manual_required=0, errors=0
    ) == "incomplete"


def test_ai_off_with_ai_requiring_rules_is_incomplete():
    # AI 规则被跳过 → incomplete_rules>0；即使没有任何 error 也不能 pass
    assert conclude_review(
        no_executable_rules=False, incomplete_rules=1, manual_required=0, errors=0
    ) == "incomplete"


def test_incomplete_keeps_deterministic_errors_visible_as_incomplete():
    # 存在确定性错误 + 检查不完整 → incomplete（不把错误伪装成单纯 needs_revision）
    assert conclude_review(
        no_executable_rules=False, incomplete_rules=1, manual_required=0, errors=3
    ) == "incomplete"


def test_manual_required_never_passes():
    assert conclude_review(
        no_executable_rules=False, incomplete_rules=0, manual_required=1, errors=0
    ) == "incomplete"


def test_no_issue_but_incomplete_review_is_not_pass():
    # done 只是执行结束；没有 issue 也不等于完成了有效审查
    assert conclude_review(
        no_executable_rules=False, incomplete_rules=1, manual_required=0, errors=0
    ) == "incomplete"
    assert conclude_review(
        no_executable_rules=False, incomplete_rules=0, manual_required=1, errors=0
    ) == "incomplete"


def test_clean_complete_review_is_pass():
    assert conclude_review(
        no_executable_rules=False, incomplete_rules=0, manual_required=0, errors=0
    ) == "pass"


def test_deterministic_errors_are_needs_revision():
    assert conclude_review(
        no_executable_rules=False, incomplete_rules=0, manual_required=0, errors=2
    ) == "needs_revision"
