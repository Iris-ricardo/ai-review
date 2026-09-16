"""Unit tests for eval/metrics.py — dual-scope metric contract (batch B1) + R09 身份口径.

B1：旧集合口径合并重复 (checker, severity) 实例，新实例口径逐实例一对一计数，零分母报不适用而非误导性 0。
R09：同类同级内按标注身份（目标字段 + 证据锚点）配对，错误位置不得互相顶替；锚点用关键词而非 message 全等，页码差异只记提示。"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from eval.metrics import (  # noqa: E402
    anchors_satisfied,
    checker_counts,
    classify_issue,
    instance_identity,
    match_identified,
    match_issues,
    precision_recall_f1,
    set_based_counts,
)


def _issue(checker, severity, **extra):
    data = {"checker": checker, "severity": severity}
    data.update(extra)
    return data


def test_classify_issue_groups():
    assert classify_issue(_issue("attachment_checklist", "info", confidence="manual_required")) == "manual_required"
    degraded = _issue(
        "anonymity_check", "info",
        layer="llm", message="AI 检查未执行：模型不可用",
    )
    assert classify_issue(degraded) == "degraded"
    assert classify_issue(_issue("budget_check", "error")) == "deterministic"


def test_set_based_scope_merges_duplicate_instances():
    """Legacy scope: 3 predicted + 1 expected of the same key -> 1 TP, 0 FP."""
    predicted = [_issue("layout_check", "warning") for _ in range(3)]
    expected = [_issue("layout_check", "warning")]
    assert set_based_counts(predicted, expected) == {"tp": 1, "fp": 0, "fn": 0}


def test_instance_scope_counts_duplicates_as_false_positives():
    """New scope: 3 predicted + 1 expected -> 1 TP and 2 FP (not merged)."""
    predicted = [_issue("layout_check", "warning") for _ in range(3)]
    expected = [_issue("layout_check", "warning")]
    match = match_issues(predicted, expected)
    assert match["counts"] == {"tp": 1, "fp": 2, "fn": 0}
    assert len(match["fp"]) == 2


def test_instance_scope_missing_detection_is_fn():
    predicted = [_issue("budget_check", "error")]
    expected = [
        _issue("budget_check", "error"),
        _issue("date_check", "warning"),
    ]
    match = match_issues(predicted, expected)
    assert match["counts"] == {"tp": 1, "fp": 0, "fn": 1}
    assert match["fn"][0]["checker"] == "date_check"


def test_instance_scope_severity_mismatch_is_fp_and_fn():
    predicted = [_issue("budget_check", "warning")]
    expected = [_issue("budget_check", "error")]
    match = match_issues(predicted, expected)
    assert match["counts"] == {"tp": 0, "fp": 1, "fn": 1}


def test_instance_scope_matching_is_deterministic():
    predicted = [
        _issue("layout_check", "warning", page=2, message="b"),
        _issue("layout_check", "warning", page=1, message="a"),
    ]
    expected = [_issue("layout_check", "warning")]
    first = match_issues(predicted, expected)
    second = match_issues(list(reversed(predicted)), expected)
    assert first["tp"][0][1]["page"] == second["tp"][0][1]["page"] == 1


def test_checker_counts_aggregate():
    match = match_issues(
        [_issue("budget_check", "error"), _issue("layout_check", "warning")],
        [_issue("budget_check", "error"), _issue("date_check", "warning")],
    )
    per = checker_counts(match)
    assert per["budget_check"] == {"tp": 1, "fp": 0, "fn": 0}
    assert per["layout_check"] == {"tp": 0, "fp": 1, "fn": 0}
    assert per["date_check"] == {"tp": 0, "fp": 0, "fn": 1}


def test_zero_denominator_returns_not_applicable():
    _, _, _, note = precision_recall_f1({"tp": 0, "fp": 0, "fn": 0})
    assert "不适用" in note


def test_precision_recall_math():
    p, r, f1, note = precision_recall_f1({"tp": 56, "fp": 58, "fn": 0})
    assert note == ""
    assert abs(p - 56 / 114) < 1e-9
    assert r == 1.0
    assert f1 > 0


# ── R09：身份口径（实例定位）契约 ─────────────────────────────────────


def _expected(checker, severity, target, anchors, **extra):
    data = {"checker": checker, "severity": severity, "target": target, "anchors": anchors}
    data.update(extra)
    return data


def test_instance_identity_reads_annotation_fields():
    identity = instance_identity(_expected("budget_check", "error", "合计行", ["合计"], page=1))
    assert identity == {"target": "合计行", "anchors": ["合计"], "page": 1, "locatable": True}
    # 标量锚点与缺省字段都要能读
    assert instance_identity({"anchors": "合计"})["anchors"] == ["合计"]
    assert instance_identity({})["anchors"] == []


def test_anchors_satisfied_accepts_wording_variation():
    """要求 8：真问题位置正确但措辞变化，仍应命中。"""
    expected = _expected("budget_check", "error", "合计行", ["分项合计"])
    assert anchors_satisfied(expected, _issue("budget_check", "error", message="预算勾稽不平：分项合计 33.6 ≠ 总额 30.0"))
    assert anchors_satisfied(expected, _issue("budget_check", "error", message="预算表分项合计与总额不一致（33.6 / 30.0）"))
    assert not anchors_satisfied(expected, _issue("budget_check", "error", message="预算勾稽不平"))


def test_same_rule_same_severity_wrong_location_is_not_tp():
    """R09 原始缺陷：同类同级但错误位置不同，不得记为 TP。"""
    expected = [_expected("budget_check", "error", "合计行", ["分项合计"])]
    predicted = [_issue("budget_check", "error", message="「管理费」占比 20.0% 超过上限 15%", page=99)]
    result = match_identified(predicted, expected)
    assert result["counts"] == {"tp": 0, "fp": 1, "fn": 1, "tp_identified": 0, "tp_unverified": 0}
    assert result["fn"][0]["target"] == "合计行"
    # 实例口径仍然会给 TP —— 说明旧口径不能证明找对了具体问题
    assert match_issues(predicted, expected)["counts"] == {"tp": 1, "fp": 0, "fn": 0}


def test_same_rule_different_fields_do_not_substitute():
    """要求 7：同类错误但目标字段不同，不能互相顶替。"""
    expected = [
        _expected("budget_check", "error", "管理费占比", ["管理费"]),
        _expected("budget_check", "error", "合计行", ["分项合计"]),
    ]
    predicted = [
        _issue("budget_check", "error", message="预算勾稽不平：分项合计 33.6 ≠ 总额 30.0"),
        _issue("budget_check", "error", message="「管理费」占比 20.0% 超过上限 15%"),
    ]
    result = match_identified(predicted, expected)
    assert result["counts"]["tp_identified"] == 2
    assert result["counts"]["fp"] == 0 and result["counts"]["fn"] == 0
    paired_targets = {exp["target"] for exp, _ in result["tp_identified"]}
    assert paired_targets == {"管理费占比", "合计行"}


def test_duplicate_predictions_are_false_positives():
    expected = [_expected("date_check", "warning", "起止顺序", ["不早于结束日期"])]
    predicted = [
        _issue("date_check", "warning", message="研究期限日期异常：开始日期 2024年9月 不早于结束日期"),
        _issue("date_check", "warning", message="研究期限日期异常：开始日期 2024年9月 不早于结束日期"),
    ]
    result = match_identified(predicted, expected)
    assert result["counts"] == {"tp": 1, "fp": 1, "fn": 0, "tp_identified": 1, "tp_unverified": 0}


def test_many_expected_one_prediction_is_fn():
    expected = [
        _expected("budget_check", "error", "管理费占比", ["管理费"]),
        _expected("budget_check", "error", "合计行", ["分项合计"]),
    ]
    predicted = [_issue("budget_check", "error", message="「管理费」占比 20.0% 超过上限 15%")]
    result = match_identified(predicted, expected)
    assert result["counts"]["tp_identified"] == 1
    assert result["counts"]["fn"] == 1
    assert result["fn"][0]["target"] == "合计行"


def test_missing_detection_is_fn_and_extra_is_fp():
    expected = [_expected("figure_table_numbering", "warning", "图2 题注", ["图2"])]
    assert match_identified([], expected)["counts"]["fn"] == 1
    result = match_identified([_issue("figure_table_numbering", "warning", message="图3 题注缺失")], expected)
    assert result["counts"] == {"tp": 0, "fp": 1, "fn": 1, "tp_identified": 0, "tp_unverified": 0}


def test_instances_without_anchors_are_unverified_not_identified():
    """要求 9：无法提供锚点的实例只做规则级核对，单独计数。"""
    expected = [_expected("page_limit", "error", "总页数上限", [])]
    predicted = [_issue("page_limit", "error", message="总页数超出限制（12页 > 10页）")]
    result = match_identified(predicted, expected)
    assert result["counts"] == {"tp": 1, "fp": 0, "fn": 0, "tp_identified": 0, "tp_unverified": 1}
    assert result["tp_unverified"][0][0]["target"] == "总页数上限"


def test_page_shift_is_advisory_not_blocking():
    """要求 4：合理分页变化（差 1 页）仍算命中，不产生 page_mismatch。"""
    expected = [_expected("signature_page", "error", "签字盖章页", ["签字盖章"], page=16)]
    predicted = [_issue("signature_page", "error", message="末页缺少签字盖章关键字：签字", page=17)]
    result = match_identified(predicted, expected)
    assert result["counts"]["tp_identified"] == 1
    assert result["page_mismatches"] == []


def test_large_page_shift_is_reported_but_still_tp():
    expected = [_expected("signature_page", "error", "签字盖章页", ["签字盖章"], page=3)]
    predicted = [_issue("signature_page", "error", message="末页缺少签字盖章关键字：签字", page=99)]
    result = match_identified(predicted, expected)
    assert result["counts"]["tp_identified"] == 1
    assert result["page_mismatches"][0]["expected_page"] == 3
    assert result["page_mismatches"][0]["actual_page"] == 99


def test_zero_denominator_identity_scope_is_not_applicable():
    _, _, _, note = precision_recall_f1({"tp": 0, "fp": 0, "fn": 0})
    assert "不适用" in note
    assert match_identified([], [])["counts"]["tp_identified"] == 0


def test_annotations_match_legacy_instance_counts():
    """身份标注与旧期望的实例数必须一致（防止两套口径漂移）。"""
    from eval.inject_errors import expected_instances_for, expected_issues_for

    for numbers in ([1], [2], [3], [4], [5], [6], [7], [8], [9], [10], [3, 7], [1, 4, 9]):
        legacy = expected_issues_for(numbers)
        annotated = expected_instances_for(numbers)
        assert len(legacy) == len(annotated), f"注入 {numbers} 的实例数不一致"
        legacy_keys = sorted((item["checker"], item["severity"]) for item in legacy)
        annotated_keys = sorted((item["checker"], item["severity"]) for item in annotated)
        assert legacy_keys == annotated_keys


def test_every_annotation_has_target_and_reason():
    """要求 3/11：每个实例都要有目标字段，且锚点必须写明样本依据。"""
    from eval.inject_errors import INSTANCE_ANNOTATIONS

    for number, items in INSTANCE_ANNOTATIONS.items():
        assert items, f"注入 {number} 没有实例标注"
        for item in items:
            assert item["target"], f"注入 {number} 缺 target"
            assert item["why"], f"注入 {number} 缺依据说明"
            assert isinstance(item["anchors"], list)
