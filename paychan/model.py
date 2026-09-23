"""公开数据结构。

数值口径
--------
所有金额均为非负整数（最小记账单位），费用
``fee(x) = base_fee + ceil(x * ppm / 1_000_000)`` 全部整数精确计算，
库内不出现浮点金额。

数据结构
--------
* :class:`Edge`        —— 一条有向支付通道：余额与费用策略。
* :class:`Network`     —— 有向图（不支持平行边；允许反向两条边）。
* :class:`HopBill`     —— 某条路径上一跳的实际账单（转发额 + 费）。
* :class:`PathPlan`    —— 一条简单路径：接收量、源支付、逐跳账单。
* :class:`SplitResult` —— 可行的最优分拆（可能含 0~2 条零额占位路径）。
* :class:`InfeasibleResult` —— 已穷举证明不存在可行分拆，附瓶颈诊断。
* :class:`Bottleneck`  —— 不可行/受限时的容量瓶颈定位。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Hashable, Iterable, Mapping

from .errors import (
    DuplicateEdgeError,
    InvalidInputError,
    NetworkLimitError,
    UnknownNodeError,
)

# ---- 本题契约硬上限 -------------------------------------------------------
MAX_NODES = 8
MAX_EDGES = 16
MAX_PATHS = 3
MAX_AMOUNT = 100
PPM_DENOMINATOR = 1_000_000

_NODE_KINDS = ("source", "destination")


def _check_int(value: object, locator: str) -> int:
    """整数参数校验：拒绝 bool、拒绝 NaN/Infinity（float 非有限值）。

    允许 ``int`` 子类；接受有限 ``float`` 但不做静默舍入——只有值恰为整数
    的 float 才按该整数使用，非整数 float 直接报错（不静默纠正）。
    """
    if isinstance(value, bool):
        raise InvalidInputError(locator, f"must be int, got bool ({value!r})")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidInputError(
                locator, f"must be finite, got non-finite float ({value!r})"
            )
        if not value.is_integer():
            raise InvalidInputError(
                locator, f"must be a whole number, got fractional float ({value!r})"
            )
        return int(value)
    raise InvalidInputError(
        locator, f"must be int, got {type(value).__name__} ({value!r})"
    )


def _check_nonneg_int(value: object, locator: str) -> int:
    ivalue = _check_int(value, locator)
    if ivalue < 0:
        raise InvalidInputError(locator, f"must be >= 0, got {ivalue}")
    return ivalue


def _check_label(value: object, locator: str) -> Hashable:
    if not isinstance(value, Hashable) or isinstance(value, (bool, float)):
        # bool/float 虽可哈希，但作为节点标签语义含糊，统一拒绝。
        raise InvalidInputError(
            locator,
            f"node label must be a hashable, non-bool/non-float key, got "
            f"{type(value).__name__}",
        )
    return value


@dataclass(frozen=True)
class Edge:
    """一条有向边 ``src -> dst``。

    :param src:      起点节点标签（int/str/tuple 等可哈希键，不接受 bool/float）。
    :param dst:      终点节点标签。
    :param balance:  通道在该方向上的余额（>=0 整数），按实际转发额累计占用。
    :param base_fee: 每笔转发固定费 ``b``（>=0 整数），经此边一次计一次。
    :param ppm:      比例费率 ``r``（>=0 整数，百万分比），按 ceil 向上取整。
    """

    src: Hashable
    dst: Hashable
    balance: int
    base_fee: int = 0
    ppm: int = 0

    def __post_init__(self) -> None:
        # dataclass(frozen=True) 下用 object.__setattr__ 不适用——这里只校验。
        _check_label(self.src, "edge.src")
        _check_label(self.dst, "edge.dst")
        _check_nonneg_int(self.balance, "edge.balance")
        _check_nonneg_int(self.base_fee, "edge.base_fee")
        _check_nonneg_int(self.ppm, "edge.ppm")
        if self.src == self.dst:
            raise InvalidInputError("edge", f"self-loop edges are not allowed: {self.src!r}")

    def fee_for(self, amount: int) -> int:
        """转发 ``amount`` 单位时边收取的费用（精确整数，ceil 向上取整）。"""
        if amount < 0:
            raise InvalidInputError("amount", f"must be >= 0, got {amount}")
        return self.base_fee + (amount * self.ppm + PPM_DENOMINATOR - 1) // PPM_DENOMINATOR


class Network:
    """有向支付网络（不可变值对象，构造时完成全部校验）。

    余额占用语义：若多条路径共用同一条有向边，各路径在此边上的**实际转发额**
    （不含费）之和不得超过该边 ``balance``。费由收款方一侧沿路径逐跳另行承担，
    不占用通道余额（详见模块/包说明）。
    """

    __slots__ = ("_edges", "_adj", "_nodes", "_index")

    def __init__(self, edges: Iterable[Edge | Mapping[str, object] | tuple] = ()) -> None:
        raw: list[Edge] = []
        for i, item in enumerate(edges):
            loc = f"edges[{i}]"
            try:
                if isinstance(item, Edge):
                    edge = item
                elif isinstance(item, Mapping):
                    try:
                        edge = Edge(
                            src=item["src"],
                            dst=item["dst"],
                            balance=item.get("balance", 0),
                            base_fee=item.get("base_fee", 0),
                            ppm=item.get("ppm", 0),
                        )
                    except KeyError as exc:
                        raise InvalidInputError(
                            loc, f"missing required field {exc.args[0]!r}"
                        ) from None
                elif isinstance(item, tuple):
                    try:
                        edge = Edge(*item)  # type: ignore[arg-type]
                    except TypeError as exc:
                        raise InvalidInputError(
                            loc,
                            f"tuple edge must be (src, dst, balance[, base_fee[, ppm]]): {exc}",
                        ) from None
                else:
                    raise InvalidInputError(
                        loc, f"edge must be Edge/dict/tuple, got {type(item).__name__}"
                    )
            except InvalidInputError as exc:
                # 统一加上 edges[i] 前缀，保证错误可定位到具体元素与字段。
                if exc.locator.startswith("edge."):
                    field = exc.locator.split(".", 1)[1]
                    raise InvalidInputError(f"{loc}.{field}", exc.message) from None
                if exc.locator == "edge":  # 自环
                    raise InvalidInputError(loc, exc.message) from None
                raise
            raw.append(edge)

        if len(raw) > MAX_EDGES:
            raise NetworkLimitError(
                "edges", f"too many directed edges: {len(raw)} > {MAX_EDGES}"
            )
        nodes: set[Hashable] = set()
        for e in raw:
            nodes.add(e.src)
            nodes.add(e.dst)
        if len(nodes) > MAX_NODES:
            raise NetworkLimitError(
                "nodes", f"too many distinct nodes: {len(nodes)} > {MAX_NODES}"
            )

        index: dict[tuple[Hashable, Hashable], int] = {}
        adj: dict[Hashable, list[int]] = {n: [] for n in nodes}
        for i, e in enumerate(raw):
            key = (e.src, e.dst)
            if key in index:
                raise DuplicateEdgeError(
                    f"edges[{i}]", f"duplicate directed edge {e.src!r} -> {e.dst!r}"
                )
            index[key] = i
            adj[e.src].append(i)

        self._edges: tuple[Edge, ...] = tuple(raw)
        self._adj = {n: tuple(ids) for n, ids in adj.items()}
        self._nodes = frozenset(nodes)
        self._index = index

    # -- 只读视图 -----------------------------------------------------------
    @property
    def edges(self) -> tuple[Edge, ...]:
        return self._edges

    @property
    def nodes(self) -> frozenset[Hashable]:
        return self._nodes

    def out_ids(self, node: Hashable) -> tuple[int, ...]:
        if node not in self._nodes:
            raise UnknownNodeError("node", f"unknown node label {node!r}")
        return self._adj[node]

    def edge_id(self, src: Hashable, dst: Hashable) -> int | None:
        return self._index.get((src, dst))

    def require_node(self, node: object, kind: str) -> Hashable:
        label = _check_label(node, kind)
        if label not in self._nodes:
            raise UnknownNodeError(kind, f"unknown node label {label!r}")
        return label

    def __len__(self) -> int:
        return len(self._edges)

    def __repr__(self) -> str:
        return f"Network({list(self._edges)!r})"


@dataclass(frozen=True)
class HopBill:
    """路径上一跳（一条边）的逐跳账单。

    沿路径方向 ``src -> dst``：
      * ``amount``：此边必须**转发给下一跳**的金额（= 下游所有费用累加后的口径）；
      * ``fee``：此边收取的费 ``base_fee + ceil(amount*ppm/1e6)``；
      * ``inflow``：此跳上游必须送入的金额 = ``amount + fee``；
      * ``balance_used``：本跳占用的通道余额（按契约按实际发送额计，即 ``amount``）。
    """

    edge_index: int
    src: Hashable
    dst: Hashable
    amount: int
    fee: int
    inflow: int
    balance_used: int


@dataclass(frozen=True)
class PathPlan:
    """一条简单路径的执行计划。

    :param nodes:     路径节点序列 ``[source, ..., destination]``。
    :param edge_ids:  逐跳边索引序列，长度 = ``len(nodes)-1``。
    :param deliver:   本路径在目的地实际交付的非负整数金额。
    :param source_paid: 源在本路径上支付的总金额。
    :param hops:      逐跳账单（沿路径方向）。
    """

    nodes: tuple[Hashable, ...]
    edge_ids: tuple[int, ...]
    deliver: int
    source_paid: int
    hops: tuple[HopBill, ...]

    @property
    def is_active(self) -> bool:
        """交付额 > 0 的路径才计为“使用的路径”。"""
        return self.deliver > 0

    def total_fee(self) -> int:
        return self.source_paid - self.deliver


@dataclass(frozen=True)
class Bottleneck:
    """不可行诊断中的一条瓶颈割边。

    该边位于以余额为容量松弛求出的 s-t 最小割上：所有交付都必须跨过这些边，
    而它们的余额总和不足（``cut_capacity < amount``）。注意这是必要条件诊断
    （忽略逐跳费叠加、ceil 取整与最多 3 路径限制），真正的不可行结论来自
    主搜索的完整穷举。

    :param balance: 该边自身的转发额容量（即通道余额）。
    :param cut_capacity: 所属割全部割边的余额之和（诊断口径）。
    :param required: 交付需求 D。
    :param margin: 缺口 ``required - cut_capacity``（>0 表示不足）。
    """

    edge_index: int
    src: Hashable
    dst: Hashable
    balance: int
    cut_capacity: int
    required: int
    margin: int


@dataclass(frozen=True)
class SplitResult:
    """可行的最优分拆结果。

    选择优先级：源总支付最低 → 使用路径数最少（仅计 ``deliver>0``）→
    路径序列字典序最小（节点标签序列逐个比较）。

    :param plans: 1..3 条 :class:`PathPlan`，全部交付额 >0 且按路径字典序排列；
                  D=0 时为空元组。
    """

    plans: tuple[PathPlan, ...]
    source: Hashable
    destination: Hashable
    amount: int

    @property
    def amount_delivered(self) -> int:
        return sum(p.deliver for p in self.plans)

    @property
    def feasible(self) -> bool:
        return True

    @property
    def source_total_paid(self) -> int:
        return sum(p.source_paid for p in self.plans)

    @property
    def paths_used(self) -> int:
        return sum(1 for p in self.plans if p.is_active)

    @property
    def total_fee(self) -> int:
        return self.source_total_paid - self.amount_delivered

    def edge_usage(self) -> dict[int, int]:
        """每条边被实际转发额累计占用的余额 ``{edge_index: used}``（只含有占用的）。"""
        used: dict[int, int] = {}
        for p in self.plans:
            for h in p.hops:
                if h.balance_used:
                    used[h.edge_index] = used.get(h.edge_index, 0) + h.balance_used
        return used


@dataclass(frozen=True)
class InfeasibleResult:
    """已证明不存在可行分拆（在预算内完成了穷举）。

    :param reason: 机器可读原因：``"no_path"``（源不可达目的地）或
                   ``"insufficient_capacity"``（有路但余额无法承载 D）。
    :param bottlenecks: 诊断信息：``no_path`` 时为空；容量不足时给出相关 s-t
                        割/路径上的关键受限边（诊断用途，非完整最小割证明）。
    """

    source: Hashable
    destination: Hashable
    amount: int
    reason: str
    bottlenecks: tuple[Bottleneck, ...] = field(default_factory=tuple)

    @property
    def feasible(self) -> bool:
        return False
