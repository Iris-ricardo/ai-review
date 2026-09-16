"""评测指标：三种口径并列（纯 stdlib，后端测试可直接导入）；阈值判定仍用实例口径（未更改验收标准）。
- 旧口径：按 (checker, severity) 集合去重，同规则同级别多个问题合并为 1（历史基线）。
- 实例口径：每个问题实例一对一计数，重复计入 FP/FN；只证明「条数对不对」，不证明「找对了具体问题」。
- 身份口径（R09）：同类同级内按标注身份比对——预期实例带 anchors（证据锚点，取自注入语义而非输出反填）时，预测的 evidence/message 须命中全部锚点才算 tp_identified，命中不了记 FN、配不上记 FP，同类不同字段/位置不得互相顶替；无锚点实例按规则级配对单列 tp_unverified；页码差异仅作提示（page_mismatches），不作配对硬条件。"""
from __future__ import annotations

from typing import Callable


def issue_key(item: dict) -> tuple[str, str]:
    return (str(item.get("checker") or ""), str(item.get("severity") or ""))


def classify_issue(issue: dict) -> str:
    """Group an issue into deterministic / manual_required / degraded."""
    if issue.get("confidence") == "manual_required":
        return "manual_required"
    if (
        issue.get("layer") == "llm"
        and issue.get("severity") == "info"
        and str(issue.get("message", "")).startswith("AI 检查未执行")
    ):
        return "degraded"
    return "deterministic"


def _sort_key(item: dict) -> tuple:
    return (
        issue_key(item),
        str(item.get("page") or ""),
        str(item.get("message") or ""),
        str(item.get("rule_id") or ""),
    )


def match_issues(predicted: list[dict], expected: list[dict]) -> dict:
    """One-to-one greedy matching per (checker, severity) key; both sides sorted, so
    pairing is reproducible and each instance pairs at most once. Returns
    ``{"tp": [(expected, actual)], "fp": [...], "fn": [...], "counts": {...}}``."""
    pred_by_key: dict[tuple[str, str], list[dict]] = {}
    exp_by_key: dict[tuple[str, str], list[dict]] = {}
    for item in sorted(predicted, key=_sort_key):
        pred_by_key.setdefault(issue_key(item), []).append(item)
    for item in sorted(expected, key=_sort_key):
        exp_by_key.setdefault(issue_key(item), []).append(item)

    tp: list[tuple[dict, dict]] = []
    fp: list[dict] = []
    fn: list[dict] = []
    for key in sorted(set(pred_by_key) | set(exp_by_key)):
        preds = pred_by_key.get(key, [])
        exps = exp_by_key.get(key, [])
        tp.extend(zip(exps, preds))
        fp.extend(preds[len(exps):])
        fn.extend(exps[len(preds):])
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "counts": {"tp": len(tp), "fp": len(fp), "fn": len(fn)},
    }


def set_based_counts(predicted: list[dict], expected: list[dict]) -> dict[str, int]:
    """Legacy scope: dedupe by (checker, severity) before set arithmetic."""
    pset = {issue_key(item) for item in predicted}
    eset = {issue_key(item) for item in expected}
    return {
        "tp": len(pset & eset),
        "fp": len(pset - eset),
        "fn": len(eset - pset),
    }


def checker_counts(match: dict) -> dict[str, dict[str, int]]:
    """Aggregate a match result per checker (instance-level scope)."""
    per: dict[str, dict[str, int]] = {}
    for expected, actual in match["tp"]:
        per.setdefault(issue_key(expected)[0], {"tp": 0, "fp": 0, "fn": 0})["tp"] += 1
    for actual in match["fp"]:
        per.setdefault(issue_key(actual)[0], {"tp": 0, "fp": 0, "fn": 0})["fp"] += 1
    for expected in match["fn"]:
        per.setdefault(issue_key(expected)[0], {"tp": 0, "fp": 0, "fn": 0})["fn"] += 1
    return per


def precision_recall_f1(counts: dict[str, int]) -> tuple[float, float, float, str]:
    """Return (precision, recall, f1, note); zero denominators say 不适用."""
    tp, fp, fn = counts.get("tp", 0), counts.get("fp", 0), counts.get("fn", 0)
    if tp + fp + fn == 0:
        return 0.0, 0.0, 0.0, "不适用（无样本）"
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1, ""


# ── R09：问题实例身份（目标字段 / 证据锚点）─────────────────────────────

#: 预测侧可用于核对的文本字段（按可信度排序）
_ANCHOR_TEXT_FIELDS = ("evidence", "message", "suggestion", "block_id")


def instance_identity(item: dict) -> dict:
    """读取（预期）实例身份：目标字段、证据锚点、可选页码、是否可定位。

    身份取自**标注**（inject_errors 注入语义），不从实际输出反填。"""
    anchors = item.get("anchors") or []
    if isinstance(anchors, str):
        anchors = [anchors]
    return {
        "target": str(item.get("target") or ""),
        "anchors": [str(value) for value in anchors if str(value)],
        "page": item.get("page"),
        "locatable": bool(item.get("locatable", True)),
    }


def anchor_text(item: dict) -> str:
    """预测实例的可核对文本（evidence 优先，其次 message 等）。"""
    return " ".join(str(item.get(field) or "") for field in _ANCHOR_TEXT_FIELDS)


def anchors_satisfied(expected: dict, actual: dict) -> bool:
    """预期实例的全部锚点是否都能在预测实例的文本中找到。

    用「关键词全部出现」而非 message 全等，允许合理措辞变化。"""
    anchors = instance_identity(expected)["anchors"]
    if not anchors:
        return False
    text = anchor_text(actual)
    return all(anchor in text for anchor in anchors)


def match_identified(predicted: list[dict], expected: list[dict]) -> dict:
    """身份口径匹配：同类同级内先按证据锚点定位，剩余做规则级配对。

    返回 tp_identified / tp_unverified / fp / fn / tp（前两类之和）/ counts；page_mismatches 仅作位置提示，不参与配对。"""
    pred_by_key: dict[tuple[str, str], list[dict]] = {}
    exp_by_key: dict[tuple[str, str], list[dict]] = {}
    for item in sorted(predicted, key=_sort_key):
        pred_by_key.setdefault(issue_key(item), []).append(item)
    for item in sorted(expected, key=_sort_key):
        exp_by_key.setdefault(issue_key(item), []).append(item)

    tp_identified: list[tuple[dict, dict]] = []
    tp_unverified: list[tuple[dict, dict]] = []
    fp: list[dict] = []
    fn: list[dict] = []
    page_mismatches: list[dict] = []

    for key in sorted(set(pred_by_key) | set(exp_by_key)):
        preds = list(pred_by_key.get(key, []))
        exps = list(exp_by_key.get(key, []))
        locatable = [item for item in exps if instance_identity(item)["anchors"]]
        rule_level = [item for item in exps if not instance_identity(item)["anchors"]]

        # ① 可定位预期：只有锚点命中的预测才能配对，配不上就是 FN（不得被同类顶替）
        for exp in locatable:
            hit_index = next(
                (index for index, pred in enumerate(preds) if anchors_satisfied(exp, pred)),
                None,
            )
            if hit_index is None:
                fn.append(exp)
                continue
            actual = preds.pop(hit_index)
            tp_identified.append((exp, actual))
            _record_page_mismatch(exp, actual, page_mismatches)

        # ② 规则级预期（标注显式声明不可定位）：与剩余预测一对一配对，单独计数
        for _ in range(min(len(rule_level), len(preds))):
            exp = rule_level.pop(0)
            actual = preds.pop(0)
            tp_unverified.append((exp, actual))
            _record_page_mismatch(exp, actual, page_mismatches)

        fn.extend(rule_level)
        fp.extend(preds)

    tp = tp_identified + tp_unverified
    return {
        "tp": tp,
        "tp_identified": tp_identified,
        "tp_unverified": tp_unverified,
        "fp": fp,
        "fn": fn,
        "page_mismatches": page_mismatches,
        "counts": {
            "tp": len(tp),
            "fp": len(fp),
            "fn": len(fn),
            "tp_identified": len(tp_identified),
            "tp_unverified": len(tp_unverified),
        },
    }


def _record_page_mismatch(expected: dict, actual: dict, sink: list[dict]) -> None:
    """记录页码位移提示（>1 页才算），仅作报告用，不影响 TP/FP/FN。"""
    expected_page = instance_identity(expected)["page"]
    actual_page = actual.get("page")
    if not isinstance(expected_page, int) or not isinstance(actual_page, int):
        return
    if abs(expected_page - actual_page) > 1:
        sink.append({
            "checker": issue_key(expected)[0],
            "severity": issue_key(expected)[1],
            "target": instance_identity(expected)["target"],
            "expected_page": expected_page,
            "actual_page": actual_page,
        })
