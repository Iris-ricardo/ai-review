"""评估标注：期望问题按实例展开（multiplicity）。

date 注入(#9)确定性产生两条 date_check 命中（日期异常 + 期限不一致），GROUND_TRUTH 带 instances=2；
expected 必须按实例数展开，否则实例级口径会把第二条真命中算成 FP。"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from eval.inject_errors import GROUND_TRUTH, expected_issues_for  # noqa: E402


def test_date_injector_expects_two_instances():
    expected = expected_issues_for([9])
    assert len(expected) == 2
    assert all(item["checker"] == "date_check" and item["severity"] == "warning" for item in expected)


def test_default_injector_expects_one_instance():
    # #2 无 extra/instances → 恰好 1 条期望
    assert expected_issues_for([2]) == [{"checker": "page_limit", "severity": "error"}]


def test_anonymity_excluded_when_requested():
    with_ai = expected_issues_for([5], include_anonymity=True)
    without_ai = expected_issues_for([5], include_anonymity=False)
    assert len(with_ai) == 1 and with_ai[0]["checker"] == "anonymity_check"
    assert without_ai == []


def test_same_key_counts_merge_across_injectors():
    """同键实例数按注入器求和（#3=1，#4=2）。

    样本组合层已排除 {3,4} 同现（run_eval._sample_errors）；此处断言函数本身“显式同时注入两者时按实例数求和”的语义。"""
    assert len(expected_issues_for([3])) == 1
    assert len(expected_issues_for([4])) == 2
    assert len(expected_issues_for([3, 4])) == 3


def test_ground_truth_entry_carries_instances():
    assert GROUND_TRUTH[9].get("instances", 1) == 2


def test_extra_expected_keys_for_side_effects():
    # #7（交换两节但保留编号）→ 1 条 section_order + 3 条 heading_numbering
    expected = expected_issues_for([7])
    assert sum(1 for e in expected if e["checker"] == "section_order") == 1
    assert sum(1 for e in expected if e["checker"] == "heading_numbering") == 3
    # #1（删除研究方法节）→ 1 条 required_sections + 1 条 heading_numbering
    expected1 = expected_issues_for([1])
    assert sum(1 for e in expected1 if e["checker"] == "required_sections") == 1
    assert sum(1 for e in expected1 if e["checker"] == "heading_numbering") == 1
