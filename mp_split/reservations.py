"""有限预留事务层：prepare / commit / abort 与逻辑时钟到期。

在 :mod:`mp_split` 的无状态求解器之上加一层**有状态账本**
:class:`ReservationLedger`：

* `prepare` 用“余额减去全部有效预留后的可用量”调用原有
  :func:`mp_split.solve` 计算路径，成功后按逐边**实际发送额**
  （`edge_usages[].sent_total`）占住容量，但不扣最终余额；
* `commit` 把该预留的逐边占用结算为已扣（settled），余额真正减少；
* `abort` 或逻辑时钟到期（`now >= expires_at`）释放占用；
* 到期与 commit 同刻时**先处理到期**，已过期记录不能复活；
* 每个请求有唯一 `request_id`：同参数重复调用返回原状态（幂等），
  同 ID 异参数拒绝（:class:`ReservationConflictError`）；
* 终态（committed/aborted/expired）上的重复同向操作是幂等 no-op，
  不重复扣还余额；反向操作抛 :class:`ReservationStateError`；
* `prepare` 失败是原子的：不留下任何部分边占用。

守恒不变量（:meth:`ReservationLedger.audit` 独立核对）::

    对每条边：balance == settled + reserved_active + available

其中 `reserved_active` 由全部 held 记录的逐边占用独立重算，
`settled` 只增不减。费率取整沿用 :mod:`mp_split.fees` 的精确整数
语义，本层不引入任何浮点。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import (
    NetworkValidationError,
    ReservationConflictError,
    ReservationStateError,
    UnknownReservationError,
)
from .model import Edge, Network, SplitResult, _is_real_int
from .solver import DEFAULT_BUDGET, solve

HELD = "held"
COMMITTED = "committed"
ABORTED = "aborted"
EXPIRED = "expired"
TERMINAL = (COMMITTED, ABORTED, EXPIRED)


@dataclass(frozen=True)
class EdgeHold:
    """一条边上的预留占用（按实际发送额计）。"""

    edge: Edge
    sent: int


@dataclass(frozen=True)
class EdgeState:
    """账本对一条边的独立核对视图（原始节点编号）。"""

    edge: Edge
    settled: int
    reserved: int
    available: int


@dataclass
class Reservation:
    """一次预留请求的当前状态（快照由账本返回，status 随操作推进）。"""

    request_id: str
    source: int
    destination: int
    delivery: int
    max_paths: int
    created_at: int
    expires_at: int
    status: str
    result: SplitResult | None
    holds: tuple[EdgeHold, ...]
    failure: str | None = None


@dataclass
class _Record:
    """账本内部记录（含幂等指纹与逐边内部索引占用）。"""

    fingerprint: tuple
    reservation: Reservation
    # 内部边索引 -> 占用实发额
    holds_internal: dict[int, int] = field(default_factory=dict)


class ReservationLedger:
    """有向支付通道的有限预留账本（单线程、离线、逻辑时钟）。

    参数:
        network: 校验后的 :class:`Network`。Network 本身不可变，
            账本在其上维护 settled 与有效预留。
    """

    def __init__(self, network: Network) -> None:
        if not isinstance(network, Network):
            raise NetworkValidationError(
                f"network: expected Network, got {type(network).__name__}"
            )
        self._net = network
        n_edges = len(network.edges)
        self._settled = [0] * n_edges
        self._records: dict[str, _Record] = {}

    # ---- 逻辑时钟与到期 ----
    def _check_now(self, now: int) -> int:
        t = _is_real_int(now, "now")
        if t < 0:
            raise NetworkValidationError(f"now: must be >= 0, got {t}")
        return t

    def _expire_due(self, now: int) -> None:
        """把 `expires_at <= now` 的 held 记录置为 expired 并释放占用。

        每个操作入口先调用本方法：同刻的到期先于 commit/abort 处理，
        已过期记录进入终态，不能复活。
        """
        for rec in self._records.values():
            r = rec.reservation
            if r.status == HELD and r.expires_at <= now:
                r.status = EXPIRED

    def _active_holds(self) -> list[int]:
        """逐边有效预留总量（由全部 held 记录独立重算）。"""
        totals = [0] * len(self._net.edges)
        for rec in self._records.values():
            if rec.reservation.status == HELD:
                for ei, sent in rec.holds_internal.items():
                    totals[ei] += sent
        return totals

    def _effective_network(self, now: int) -> Network:
        """余额减去已结算与全部有效预留后的可用网络。"""
        reserved = self._active_holds()
        net = self._net
        edges = []
        for i, e in enumerate(net.edges):
            avail = e.balance - self._settled[i] - reserved[i]
            assert avail >= 0, "conservation invariant violated"
            edges.append(
                Edge(
                    net.to_label(e.src),
                    net.to_label(e.dst),
                    avail,
                    e.base_fee,
                    e.ppm,
                )
            )
        return Network(edges, node_count=net.node_count)

    # ---- 公开操作 ----
    def prepare(
        self,
        request_id: str,
        source: int,
        destination: int,
        delivery: int,
        *,
        max_paths: int = 3,
        ttl: int,
        now: int = 0,
        budget: int = DEFAULT_BUDGET,
    ) -> Reservation:
        """计算路径并占住相应边容量（不扣最终余额）。

        `ttl` 为逻辑时钟有效期：`expires_at = now + ttl`，
        到期时刻与 commit 同刻时先处理到期。同 `request_id` 同参数
        重复调用返回原状态；同 ID 异参数抛
        :class:`ReservationConflictError`。求解失败（不可行）不留下
        任何部分边占用，失败状态同样按 ID 幂等记忆。
        """
        if not isinstance(request_id, str) or not request_id:
            raise NetworkValidationError(
                f"request_id: expected a non-empty str, got {request_id!r}"
            )
        t = self._check_now(now)
        ttl_v = _is_real_int(ttl, "ttl")
        if ttl_v <= 0:
            raise NetworkValidationError(f"ttl: must be > 0, got {ttl_v}")
        mp = _is_real_int(max_paths, "max_paths")
        d = _is_real_int(delivery, "delivery")
        # source/destination/delivery/max_paths/budget 的完整契约由
        # solve 在有效网络上校验；此处先整形以构造幂等指纹。
        fingerprint = (source, destination, d, mp, ttl_v, t)

        existing = self._records.get(request_id)
        if existing is not None:
            if existing.fingerprint != fingerprint:
                raise ReservationConflictError(request_id)
            self._expire_due(t)
            return existing.reservation

        self._expire_due(t)
        effective = self._effective_network(t)
        result = solve(effective, source, destination, d, max_paths=mp, budget=budget)

        holds_internal: dict[int, int] = {}
        holds: list[EdgeHold] = []
        failure: str | None = None
        if result.feasible:
            # 原子占用：先在本地累计，全部合法后才登记记录；
            # solve 已保证 sent_total <= 可用余额，故不会部分占用。
            for usage in result.edge_usages:
                ei = self._edge_index(usage.edge.src, usage.edge.dst)
                holds_internal[ei] = holds_internal.get(ei, 0) + usage.sent_total
                holds.append(EdgeHold(usage.edge, usage.sent_total))
        else:
            failure = (
                f"{result.bottleneck.kind}: {result.bottleneck.detail}"
                if result.bottleneck is not None
                else "infeasible"
            )

        reservation = Reservation(
            request_id=request_id,
            source=source,
            destination=destination,
            delivery=d,
            max_paths=mp,
            created_at=t,
            expires_at=t + ttl_v,
            status=HELD if result.feasible else ABORTED,
            result=result if result.feasible else None,
            holds=tuple(holds),
            failure=failure,
        )
        self._records[request_id] = _Record(fingerprint, reservation, holds_internal)
        return reservation

    def commit(self, request_id: str, *, now: int) -> Reservation:
        """结算预留：逐边占用转为已扣余额。

        到期先处理：`now >= expires_at` 时记录已过期，抛
        :class:`ReservationStateError`（已过期不能复活）。终态重复
        commit 是幂等 no-op，不重复扣余额。
        """
        t = self._check_now(now)
        rec = self._get(request_id)
        self._expire_due(t)
        r = rec.reservation
        if r.status == COMMITTED:
            return r
        if r.status != HELD:
            raise ReservationStateError(request_id, r.status, "commit")
        for ei, sent in rec.holds_internal.items():
            self._settled[ei] += sent
        rec.holds_internal = {}
        r.status = COMMITTED
        return r

    def abort(self, request_id: str, *, now: int) -> Reservation:
        """释放预留：占用返还可用量，不扣余额。

        终态重复 abort 是幂等 no-op，不重复返还；对已 committed
        记录 abort 抛 :class:`ReservationStateError`。
        """
        t = self._check_now(now)
        rec = self._get(request_id)
        self._expire_due(t)
        r = rec.reservation
        if r.status in (ABORTED, EXPIRED):
            return r
        if r.status != HELD:
            raise ReservationStateError(request_id, r.status, "abort")
        rec.holds_internal = {}
        r.status = ABORTED
        return r

    def reservation(self, request_id: str, *, now: int | None = None) -> Reservation:
        """查询请求当前状态；`now` 给定时先按逻辑时钟推进到期。"""
        if now is not None:
            self._expire_due(self._check_now(now))
        return self._get(request_id).reservation

    def edge_states(self, *, now: int | None = None) -> tuple[EdgeState, ...]:
        """独立账本视图：每边 settled / reserved / available。"""
        if now is not None:
            self._expire_due(self._check_now(now))
        reserved = self._active_holds()
        out: list[EdgeState] = []
        for i, e in enumerate(self._net.edges):
            avail = e.balance - self._settled[i] - reserved[i]
            out.append(
                EdgeState(
                    Edge(
                        self._net.to_label(e.src),
                        self._net.to_label(e.dst),
                        e.balance,
                        e.base_fee,
                        e.ppm,
                    ),
                    self._settled[i],
                    reserved[i],
                    avail,
                )
            )
        return tuple(out)

    def audit(self, *, now: int | None = None) -> None:
        """独立核对守恒：balance == settled + reserved + available。

        reserved 由全部 held 记录逐边重算（不依赖缓存）；任何不一致
        抛 `AssertionError`。
        """
        for st in self.edge_states(now=now):
            total = st.settled + st.reserved + st.available
            assert total == st.edge.balance, (
                f"conservation violated on edge "
                f"({st.edge.src}->{st.edge.dst}): "
                f"{st.settled} + {st.reserved} + {st.available} "
                f"!= {st.edge.balance}"
            )
            assert st.settled >= 0 and st.reserved >= 0 and st.available >= 0

    # ---- 内部 ----
    def _get(self, request_id: str) -> _Record:
        if not isinstance(request_id, str):
            raise NetworkValidationError(
                f"request_id: expected str, got {type(request_id).__name__}"
            )
        rec = self._records.get(request_id)
        if rec is None:
            raise UnknownReservationError(request_id)
        return rec

    def _edge_index(self, src_label: int, dst_label: int) -> int:
        s = self._net.to_internal(src_label, "edge.src")
        d = self._net.to_internal(dst_label, "edge.dst")
        for i, e in enumerate(self._net.edges):
            if e.src == s and e.dst == d:
                return i
        raise AssertionError(f"edge {src_label}->{dst_label} not in ledger network")

