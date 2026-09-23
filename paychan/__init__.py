"""paychan —— 有向支付通道的多路径可行分拆（离线、纯标准库）。

公开接口
--------
数据结构： :class:`Edge`、:class:`Network`、:class:`HopBill`、:class:`PathPlan`、
          :class:`SplitResult`、:class:`InfeasibleResult`、:class:`Bottleneck`。
求解入口： :func:`split_payment`。
异常：     :class:`PaymentSplitError`、:class:`InvalidInputError`、
          :class:`NetworkLimitError`、:class:`UnknownNodeError`、
          :class:`DuplicateEdgeError`、:class:`SearchBudgetExhausted`。
常量：     :data:`MAX_NODES` (8)、:data:`MAX_EDGES` (16)、
          :data:`MAX_PATHS` (3)、:data:`MAX_AMOUNT` (100)。

典型用法::

    from paychan import Edge, Network, split_payment

    net = Network([Edge("A", "B", balance=50, base_fee=1, ppm=1000)])
    result = split_payment(net, "A", "B", 30)
    print(result.source_total_paid)
"""

from __future__ import annotations

from .errors import (
    DuplicateEdgeError,
    InvalidInputError,
    NetworkLimitError,
    PaymentSplitError,
    SearchBudgetExhausted,
    UnknownNodeError,
)
from .model import (
    MAX_AMOUNT,
    MAX_EDGES,
    MAX_NODES,
    MAX_PATHS,
    PPM_DENOMINATOR,
    Bottleneck,
    Edge,
    HopBill,
    InfeasibleResult,
    Network,
    PathPlan,
    SplitResult,
)
from .solver import DEFAULT_BUDGET, simple_paths, split_payment

__all__ = [
    # 主入口
    "split_payment",
    "simple_paths",
    "DEFAULT_BUDGET",
    # 数据结构
    "Edge",
    "Network",
    "HopBill",
    "PathPlan",
    "SplitResult",
    "InfeasibleResult",
    "Bottleneck",
    # 异常
    "PaymentSplitError",
    "InvalidInputError",
    "NetworkLimitError",
    "UnknownNodeError",
    "DuplicateEdgeError",
    "SearchBudgetExhausted",
    # 常量
    "MAX_NODES",
    "MAX_EDGES",
    "MAX_PATHS",
    "MAX_AMOUNT",
    "PPM_DENOMINATOR",
]

__version__ = "1.0.0"
