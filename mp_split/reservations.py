"""离线通道的有限预留事务（prepare / commit / abort + 逻辑时钟到期）。

在 :func:`mp_split.solve` 的纯函数求解之上提供一层**有状态**的账本：

* `prepare`：按“余额 - 已结算 - 全部有效预留”的可用量重新规划路径，
  可行则占住相应边的容量（按每跳实际发送额计），但**不**扣最终余额；
  不可行则记录失败且不留任何部分边占用。
* `commit`：结算——把该预留占用的实发额永久计入对应边的已结算量。
* `abort` 或逻辑时钟到期：释放占用，容量回到可用量。

契约要点：

* 每个请求有唯一 `request_id`；同 ID 同参数重试返回**原状态**
  （幂等），同 ID 异参数抛 :class:`DuplicateRequestError`。
* 到期时刻与 commit 同刻时**先处理到期**：`now >= expires_at` 的预留
  在任何 commit/abort/prepare 之前被释放，已过期不能复活。
* 终态（committed/aborted/expired）上的重复同操作是幂等只读，
  不重复扣还余额；跨终态操作抛 :class:`ReservationStateError`。
* 守恒不变式：对每条边 `balance == settled + reserved + available`，
  由 :meth:`Ledger.audit` 独立核对。

本模块只做离线账本核对，不连接任何支付网络。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .errors import (
    DuplicateRequestError,
    NetworkValidationError,
    ReservationStateError,
    UnknownReservationError,
)
from .model import Edge, Network, SplitResult, _is_real_int
from .solver import DEFAULT_BUDGET, solve

HELD = "held"
COMMITTED = "committed"
ABORTED = "aborted"
EXPIRED = "expired"
FAILED = "failed"

TERMINAL = (COMMITTED, ABORTED, EXPIRED, FAILED)


@dataclass(frozen=True)
class EdgeHold:
    """一条边上被预留占住的容量（按实际发送额计，原始节点编号）。"""

    src: int
    dst: int
    sent: int


@dataclass(frozen=True)
class ReservationRecord:
    """一次 prepare 的不可变状态快照。

    `status` 取值：`held` / `committed` / `aborted` / `expired`
    / `failed`。`failed` 时 `result` 为不可行的
    :class:`SplitResult`（含 `bottleneck`），`holds` 为空。
    """

    request_id: str
    status: str
    source: int
    destination: int
    delivery: int
    max_paths: int
    created_at: int
    expires_at: int | None
    result: SplitResult | None
    holds: tuple[EdgeHold, ...]


@dataclass(frozen=True)
class EdgeLedger:
    """独立账本核对：一条边的守恒分解（原始节点编号）。"""

    src: int
    dst: int
    balance: int
    settled: int
    reserved: int
    available: int


def _check_request_id(request_id: object) -> str:
    if isinstance(request_id, bool) or not isinstance(request_id, str):
        raise NetworkValidationError(
            f"request_id: expected str, got {type(request_id).__name__}"
        )
    if not request_id:
        raise NetworkValidationError("request_id: must be a non-empty string")
    return request_id


def _check_now(now: object) -> int:
    value = _is_real_int(now, "now")
    if value < 0:
        raise NetworkValidationError(f"now: must be >= 0, got {value}")
    return value


class Ledger:
    """单个网络上的预留事务账本（离线、确定性、纯整数）。

    参数:
        network: 校验后的 :class:`Network`；其边余额为**总容量**，
            账本内部维护已结算与预留占用，不修改该对象。
        ttl: 预留的生存期（逻辑时钟单位，正整数）。在时刻 `t`
            创建的预留于 `t + ttl` 到期；`now >= expires_at`
            即视为已过期。

    逻辑时钟由调用方通过各方法的 `now` 参数显式传入，应为非递减
    序列；账本本身不读系统时间，保证离线确定性。
    """

    def __init__(self, network: Network, ttl: int) -> None:
        if not isinstance(network, Network):
            raise NetworkValidationError(
                f"network: expected Network, got {type(network).__name__}"
            )
        ttl_value = _is_real_int(ttl, "ttl")
        if ttl_value <= 0:
            raise NetworkValidationError(f"ttl: must be > 0, got {ttl_value}")
        self._network = network
        self._ttl = ttl_value
        self._settled: list[int] = [0] * len(network.edges)
        self._records: dict[str, ReservationRecord] = {}

    @property
    def network(self) -> Network:
        return self._network

    @property
    def ttl(self) -> int:
        return self._ttl

    # ---- 内部 ----
    def _expire(self, now: int) -> None:
        """先释放所有到期预留（在任何新操作之前，含同刻 commit）。"""
        for rid, rec in self._records.items():
            if rec.status == HELD and rec.expires_at is not None and now >= rec.expires_at:
                self._records[rid] = replace(rec, status=EXPIRED)

    def _reserved_per_edge(self) -> list[int]:
        reserved = [0] * len(self._network.edges)
        edge_index = {
            (e.src, e.dst): i for i, e in enumerate(self._network.edges)
        }
        for rec in self._records.values():
            if rec.status != HELD:
                continue
            for hold in rec.holds:
                src = self._network.to_internal(hold.src, "hold.src")
                dst = self._network.to_internal(hold.dst, "hold.dst")
                reserved[edge_index[(src, dst)]] += hold.sent
        return reserved

    def _effective_network(self) -> Network:
        reserved = self._reserved_per_edge()
        edges = []
        for i, e in enumerate(self._network.edges):
            available = e.balance - self._settled[i] - reserved[i]
            assert available >= 0, "ledger conservation invariant"
            edges.append(
                Edge(
                    self._network.to_label(e.src),
                    self._network.to_label(e.dst),
                    available,
                    e.base_fee,
                    e.ppm,
                )
            )
        return Network(edges, node_count=self._network.node_count)

    # ---- 公开操作 ----
    def prepare(
        self,
        request_id: str,
        source: int,
        destination: int,
        delivery: int,
        *,
        max_paths: int = 3,
        budget: int = DEFAULT_BUDGET,
        now: int,
    ) -> ReservationRecord:
        """计算路径并占住相应边容量（不扣最终余额）。

        同 ID 同参数重试返回首次调用的原状态（含失败记录）；同 ID
        异参数抛 :class:`DuplicateRequestError`。规划失败（不可行）
        留下 `failed` 记录但**不留任何边占用**。
        :class:`SearchBudgetExhausted` 直接向上传播且不产生记录
        （“算不动”不是业务终态）。
        """
        rid = _check_request_id(request_id)
        moment = _check_now(now)
        self._expire(moment)

        existing = self._records.get(rid)
        if existing is not None:
            params = (source, destination, delivery, max_paths)
            old = (
                existing.source,
                existing.destination,
                existing.delivery,
                existing.max_paths,
            )
            if params != old:
                raise DuplicateRequestError(rid)
            return existing

        effective = self._effective_network()
        result = solve(
            effective,
            source,
            destination,
            delivery,
            max_paths=max_paths,
            budget=budget,
        )
        if result.feasible:
            holds = tuple(
                EdgeHold(u.edge.src, u.edge.dst, u.sent_total)
                for u in result.edge_usages
            )
            record = ReservationRecord(
                rid, HELD, source, destination, delivery, max_paths,
                moment, moment + self._ttl, result, holds,
            )
        else:
            record = ReservationRecord(
                rid, FAILED, source, destination, delivery, max_paths,
                moment, None, result, (),
            )
        self._records[rid] = record
        return record

    def commit(self, request_id: str, *, now: int) -> ReservationRecord:
        """结算：把预留占用的实发额永久计入各边已结算量。

        先处理同刻到期；对已 committed 的记录重复 commit 幂等返回
        原状态（不重复扣减）；对 aborted/expired/failed 记录抛
        :class:`ReservationStateError`（已过期不能复活）。
        """
        rid = _check_request_id(request_id)
        moment = _check_now(now)
        self._expire(moment)
        rec = self._records.get(rid)
        if rec is None:
            raise UnknownReservationError(rid)
        if rec.status == COMMITTED:
            return rec
        if rec.status != HELD:
            raise ReservationStateError(rid, rec.status, "commit")
        edge_index = {
            (e.src, e.dst): i for i, e in enumerate(self._network.edges)
        }
        for hold in rec.holds:
            src = self._network.to_internal(hold.src, "hold.src")
            dst = self._network.to_internal(hold.dst, "hold.dst")
            self._settled[edge_index[(src, dst)]] += hold.sent
        record = replace(rec, status=COMMITTED)
        self._records[rid] = record
        return record

    def abort(self, request_id: str, *, now: int) -> ReservationRecord:
        """释放预留占用的容量（不结算）。

        先处理同刻到期；对已 aborted 的记录重复 abort 幂等返回原
        状态（不重复归还）；对 expired 记录返回原状态（容量已被
        时钟释放，不再二次归还）；对 committed/failed 记录抛
        :class:`ReservationStateError`。
        """
        rid = _check_request_id(request_id)
        moment = _check_now(now)
        self._expire(moment)
        rec = self._records.get(rid)
        if rec is None:
            raise UnknownReservationError(rid)
        if rec.status in (ABORTED, EXPIRED):
            return rec
        if rec.status != HELD:
            raise ReservationStateError(rid, rec.status, "abort")
        record = replace(rec, status=ABORTED)
        self._records[rid] = record
        return record

    def get(self, request_id: str) -> ReservationRecord | None:
        """按 ID 查询当前记录快照（不存在返回 `None`）。"""
        rid = _check_request_id(request_id)
        return self._records.get(rid)

    def audit(self, *, now: int) -> tuple[EdgeLedger, ...]:
        """独立账本核对：每边 `balance == settled + reserved + available`。

        先按 `now` 处理到期，再逐边分解并断言守恒不变式。
        """
        moment = _check_now(now)
        self._expire(moment)
        reserved = self._reserved_per_edge()
        rows: list[EdgeLedger] = []
        for i, e in enumerate(self._network.edges):
            available = e.balance - self._settled[i] - reserved[i]
            assert available >= 0, (
                f"conservation violated on edge "
                f"{self._network.to_label(e.src)}->{self._network.to_label(e.dst)}"
            )
            rows.append(
                EdgeLedger(
                    self._network.to_label(e.src),
                    self._network.to_label(e.dst),
                    e.balance,
                    self._settled[i],
                    reserved[i],
                    available,
                )
            )
        return tuple(rows)
