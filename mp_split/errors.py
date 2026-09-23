"""错误类型与错误语义。

所有本库抛出的错误都继承 :class:`MPSplitError`，便于调用方一次性捕获。

* :class:`NetworkValidationError` —— 输入不满足公开契约（类型、取值、
  引用完整性、规模上限）。错误信息始终包含可定位字段，例如
  ``edges[2].balance``。
* :class:`SearchBudgetExhausted` —— 搜索预算耗尽，**不**代表无解；
  与“已证明无解”（``solve`` 返回 ``feasible=False``）严格区分。
"""

from __future__ import annotations


class MPSplitError(Exception):
    """本库所有自定义错误的基类。"""


class NetworkValidationError(MPSplitError):
    """网络或求解参数不满足契约。信息中标注出错位置。"""


class SearchBudgetExhausted(MPSplitError):
    """搜索评估次数超过预算。

    这是“算不动”，不是“无解”：在题面给定上限（8 节点 / 16 边 /
    ``D<=100``）与默认预算下不会发生；预算参数仅供隔离测试与
    防御性使用。
    """

    def __init__(self, used: int, budget: int) -> None:
        self.used = used
        self.budget = budget
        super().__init__(
            f"search budget exhausted: {used} evaluations >= budget {budget}; "
            "this is NOT a proof of infeasibility"
        )
