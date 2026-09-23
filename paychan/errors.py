"""错误类型与输入校验语义。

所有输入错误都抛出 :class:`InvalidInputError`（或其子类），错误消息中带
可定位字段（如 ``edges[3].base_fee``）。搜索预算耗尽不属于输入错误，
单独使用 :class:`SearchBudgetExhausted`，与“已证明无解”的
:class:`paychan.model.InfeasibleResult` 严格区分。
"""

from __future__ import annotations


class PaymentSplitError(Exception):
    """本库所有异常的公共基类。"""


class InvalidInputError(PaymentSplitError, ValueError):
    """非法输入。

    :param locator: 可定位的字段路径，例如 ``"edges[2].ppm"`` 或 ``"amount"``。
    :param message: 具体错误说明。
    """

    def __init__(self, locator: str, message: str) -> None:
        self.locator = locator
        self.message = message
        super().__init__(f"{locator}: {message}")


class NetworkLimitError(InvalidInputError):
    """超过本题契约的输入上限（8 节点 / 16 有向边 / D<=100 / 3 路径）。"""


class UnknownNodeError(InvalidInputError):
    """引用了网络中不存在的节点标签。"""


class DuplicateEdgeError(InvalidInputError):
    """同一对 ``(src, dst)`` 出现了两条有向边（本库不支持平行边）。"""


class SearchBudgetExhausted(PaymentSplitError, RuntimeError):
    """搜索预算耗尽：未能证明最优，也未能证明无解。

    这与返回 ``InfeasibleResult``（已穷举证明无解）是两种完全不同的结果：
    抛此异常时结论未知；调大 ``budget`` 后重试可得到确定结论。
    """

    def __init__(self, spent: int, budget: int) -> None:
        self.spent = spent
        self.budget = budget
        super().__init__(
            f"search budget exhausted after {spent} evaluations "
            f"(budget={budget}); optimality and infeasibility are both unproven"
        )
