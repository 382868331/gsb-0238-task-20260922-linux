"""公开数据结构与网络校验。

金额单位始终是题面的“整数单位”，全部使用 Python ``int``，库内部不
使用浮点。

数据结构（均为不可变值对象）::

    Edge(src, dst, balance, base_fee=b, ppm=r, lock_delta=dl)
    Network(edges, node_count=None)
    HopBill(edge, delivered, fee, sent, lock)
    PathBill(path, delivered, source_paid, hops, source_lock)
    EdgeUsage(edge, delivered_total, fee_total, sent_total, paths)
    Bottleneck(kind, detail)
    SplitResult(feasible, ...)

锁定增量 ``lock_delta`` 是纯整数的期限模型（类 CLTV 增量）：每经过
一条边，上游为该跳设置的到期必须比下游至少多出该边的增量；到期沿
路径**从收款端向源端反向累计**。详见 ``solver.solve`` 的 ``max_lock``。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .errors import NetworkValidationError

MAX_NODES = 8
MAX_EDGES = 16
MAX_DELIVERY = 100
MAX_PATHS = 3


def _is_real_int(value: object, where: str) -> int:
    """要求真正的 int：bool 被拒绝（bool 是 int 子类，按契约必须拒绝）。

    NaN / Infinity 只能出现在 float 上；非有限 float 同样拒绝，且
    float 不会被静默取整——必须由调用方显式传入 int。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float):
            if not math.isfinite(value):
                raise NetworkValidationError(f"{where}: expected a finite int, got non-finite float {value!r}")
            raise NetworkValidationError(
                f"{where}: expected int, got float {value!r} (no silent rounding)"
            )
        raise NetworkValidationError(f"{where}: expected int, got {type(value).__name__}")
    return value


@dataclass(frozen=True)
class Edge:
    """一条有向支付通道边。

    属性:
        src/dst: 节点编号，非负整数，``< node_count``。
        balance: 该边可转发的金额上限，单位为“实际发送额”
            ``sent = delivered + fee``，非负整数。
        base_fee: 固定费 b，非负整数。
        ppm: 比例费 r，单位 ppm（fee 比例项 = ceil(x*r/1e6)），非负整数。
        lock_delta: 锁定增量 dl（类 CLTV 时间锁增量），非负整数。
            上游在该跳要求的到期 = 下游到期 + dl；与转发金额无关。
    """

    src: int
    dst: int
    balance: int
    base_fee: int = 0
    ppm: int = 0
    lock_delta: int = 0

    def __post_init__(self) -> None:
        where = f"edge ({self.src!r}->{self.dst!r})"
        src = _is_real_int(self.src, f"{where}.src")
        dst = _is_real_int(self.dst, f"{where}.dst")
        balance = _is_real_int(self.balance, f"{where}.balance")
        base_fee = _is_real_int(self.base_fee, f"{where}.base_fee")
        ppm = _is_real_int(self.ppm, f"{where}.ppm")
        lock_delta = _is_real_int(self.lock_delta, f"{where}.lock_delta")
        if src < 0:
            raise NetworkValidationError(f"{where}.src: must be >= 0, got {src}")
        if dst < 0:
            raise NetworkValidationError(f"{where}.dst: must be >= 0, got {dst}")
        if src == dst:
            raise NetworkValidationError(f"{where}: self-loop edges are not allowed")
        if balance < 0:
            raise NetworkValidationError(f"{where}.balance: must be >= 0, got {balance}")
        if base_fee < 0:
            raise NetworkValidationError(f"{where}.base_fee: must be >= 0, got {base_fee}")
        if ppm < 0:
            raise NetworkValidationError(f"{where}.ppm: must be >= 0, got {ppm}")
        if lock_delta < 0:
            raise NetworkValidationError(
                f"{where}.lock_delta: must be >= 0, got {lock_delta}"
            )


@dataclass(frozen=True)
class Network:
    """校验后的有向支付网络。

    允许同一有序节点对出现至多一条边；节点编号允许不连续，内部压缩到
    ``0..n-1``，公开结果同时保留原始编号。
    """

    edges: tuple[Edge, ...]
    node_count: int
    # 内部编号 -> 原始编号
    labels: tuple[int, ...]
    # 原始编号 -> 内部编号
    _index: dict[int, int] = field(compare=False, repr=False)

    def __init__(self, edges: Iterable[Edge], node_count: int | None = None) -> None:
        edges_list = list(edges)
        if not isinstance(edges_list, list):  # pragma: no cover - 防御
            raise NetworkValidationError("edges: expected an iterable of Edge")
        if len(edges_list) > MAX_EDGES:
            raise NetworkValidationError(
                f"edges: at most {MAX_EDGES} directed edges allowed, got {len(edges_list)}"
            )
        for i, e in enumerate(edges_list):
            if not isinstance(e, Edge):
                raise NetworkValidationError(f"edges[{i}]: expected Edge, got {type(e).__name__}")

        used = sorted({n for e in edges_list for n in (e.src, e.dst)})
        if node_count is None:
            labels = tuple(used)
        else:
            nc = _is_real_int(node_count, "node_count")
            if nc < 1:
                raise NetworkValidationError(f"node_count: must be >= 1, got {nc}")
            if nc > MAX_NODES:
                raise NetworkValidationError(
                    f"node_count: at most {MAX_NODES} nodes allowed, got {nc}"
                )
            if used and used[-1] >= nc:
                raise NetworkValidationError(
                    f"edges reference node {used[-1]} but node_count={nc} (nodes must be in 0..{nc - 1})"
                )
            labels = tuple(range(nc))
        if len(labels) > MAX_NODES:
            raise NetworkValidationError(
                f"network uses {len(labels)} distinct nodes; at most {MAX_NODES} allowed"
            )
        index = {label: i for i, label in enumerate(labels)}

        seen_pairs: set[tuple[int, int]] = set()
        norm_edges: list[Edge] = []
        for i, e in enumerate(edges_list):
            pair = (index[e.src], index[e.dst])
            if pair in seen_pairs:
                raise NetworkValidationError(
                    f"edges[{i}]: duplicate directed edge {e.src}->{e.dst}"
                )
            seen_pairs.add(pair)
            norm_edges.append(
                Edge(pair[0], pair[1], e.balance, e.base_fee, e.ppm, e.lock_delta)
            )

        object.__setattr__(self, "edges", tuple(norm_edges))
        object.__setattr__(self, "node_count", len(labels))
        object.__setattr__(self, "labels", labels)
        object.__setattr__(self, "_index", index)

    # ---- 内部/外部编号转换 ----
    def to_internal(self, label: int, where: str) -> int:
        _is_real_int(label, where)
        if label not in self._index:
            raise NetworkValidationError(
                f"{where}: node {label} not present in network nodes {list(self.labels)}"
            )
        return self._index[label]

    def to_label(self, internal: int) -> int:
        return self.labels[internal]

    def adjacency(self) -> list[list[tuple[int, int]]]:
        """:returns: ``adj[u] = [(v, edge_index), ...]``，按 (v, 编号) 排序。"""
        adj: list[list[tuple[int, int]]] = [[] for _ in range(self.node_count)]
        for i, e in enumerate(self.edges):
            adj[e.src].append((e.dst, i))
        for out in adj:
            out.sort()
        return adj


@dataclass(frozen=True)
class HopBill:
    """一跳账单：在某条边上转发 ``delivered``，收费 ``fee``，上游实付 ``sent``。

    ``lock`` 是**本跳上游侧**（付款方向）该笔转发的到期时刻（绝对
    时间锁高度，整数）：它等于下游跳到期加本边 ``lock_delta``；
    末跳的到期取 0 基准（见 ``solver.solve`` 的 ``max_lock``）。
    """

    edge: Edge
    delivered: int
    fee: int
    sent: int
    lock: int


@dataclass(frozen=True)
class PathBill:
    """一条路径的完整账单。

    ``path`` 使用原始节点编号；``hops`` 从收款端向源端反向递推得到，
    但按转发顺序（源 -> 收款端）排列，``hops[0].delivered`` 是源在
    第一跳实付，``hops[-1].delivered == D_path``。

    ``source_lock`` 是源在第一跳承担的到期（路径各边 ``lock_delta``
    之和，以收款端 0 为基准）；``hops[j].lock`` 为该跳上游侧到期，
    相邻跳满足 ``hops[j].lock - hops[j+1].lock == hops[j].edge.lock_delta``。
    """

    path: tuple[int, ...]
    delivered: int
    source_paid: int
    hops: tuple[HopBill, ...]
    source_lock: int


@dataclass(frozen=True)
class EdgeUsage:
    """一条边跨所有选中路径的累计实际占用（用于核对余额）。"""

    edge: Edge
    delivered_total: int
    fee_total: int
    sent_total: int
    paths: tuple[int, ...]


@dataclass(frozen=True)
class Bottleneck:
    """不可行原因。

    kind 取值：
        ``"unreachable"``        —— 源到收款端不存在任何简单路径
        ``"insufficient_balance"`` —— 存在路径，但余额（按实际发送额
            计）不足以交付 D；``detail`` 给出收紧后的最小边容量说明
        ``"deadline_exceeded"``  —— 所有路径的源端累计到期都超过
            付款人最大期限 ``max_lock``；``detail`` 给出最小可达到期
    """

    kind: str
    detail: str


@dataclass(frozen=True)
class SplitResult:
    """求解结果。

    可行时：``feasible=True``，含 ``paths``（按选择顺序）、
    ``used_paths``、``source_total``、``edge_usages``。
    不可行时：``feasible=False``，含 ``bottleneck``。
    ``evaluations`` 为搜索中评估的路径集合数（可观测性，非优化目标）。
    ``max_lock`` 回显本次求解的付款人最大期限（``None`` 表示不限制）。
    """

    feasible: bool
    source_total: int | None
    used_paths: int
    paths: tuple[PathBill, ...]
    edge_usages: tuple[EdgeUsage, ...]
    bottleneck: Bottleneck | None
    delivery: int
    evaluations: int
    max_lock: int | None = None
