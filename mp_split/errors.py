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


class ReservationError(MPSplitError):
    """预留账本相关错误的基类。"""


class UnknownReservationError(ReservationError):
    """commit/abort/查询引用了不存在的请求 ID。"""

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        super().__init__(f"unknown reservation request_id {request_id!r}")


class ReservationConflictError(ReservationError):
    """同一请求 ID 以不同参数重复 prepare（幂等冲突）。"""

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        super().__init__(
            f"request_id {request_id!r} was already used with different parameters; "
            "identical replays return the stored state, divergent ones are rejected"
        )


class ReservationStateError(ReservationError):
    """非法状态迁移（如对终态记录做反向操作，或 commit 已过期预留）。"""

    def __init__(self, request_id: str, status: str, op: str) -> None:
        self.request_id = request_id
        self.status = status
        super().__init__(
            f"cannot {op} reservation {request_id!r} in terminal state {status!r}"
        )
