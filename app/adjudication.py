"""山洪水位预警裁定核心逻辑（纯函数，不依赖数据库 / Web 框架）。

裁定规则（默认参数，可通过 Rules 覆盖）：
- 有效读数按观测时间升序重排，相邻两条间隔 > 15 分钟即断段；
- 同一段内连续 >= 3 个读数达到上升阈值（>= rise_threshold），
  且该连续段首尾相隔 >= 10 分钟时，在首次同时满足两项条件的读数处发出预警；
- 预警状态下，连续 >= 2 个读数不高于解除阈值（<= clear_threshold），
  且首尾相隔 >= 5 分钟时解除；
- 断段只重置“上升”计数；预警不因断段自动解除，解除计数可跨段延续。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional


@dataclass(frozen=True)
class Reading:
    """参与裁定的一条有效读数。

    rise_threshold / clear_threshold 给出该读数按观测时间命中的阈值版本；
    为 None 时（旧调用方 / 无版本场景）回退到 adjudicate 传入的站点基准阈值。
    threshold_version_id 记录命中的版本 id；None 表示使用登记基准值。
    """

    id: int
    observed_at: datetime  # 已统一为带时区时间（UTC）
    water_level: float
    rise_threshold: Optional[float] = None
    clear_threshold: Optional[float] = None
    threshold_version_id: Optional[int] = None

    def thresholds(self, base_rise: float, base_clear: float) -> tuple[float, float]:
        """返回本条读数实际采用的（上升，解除）阈值。"""
        rise = self.rise_threshold if self.rise_threshold is not None else base_rise
        clear = self.clear_threshold if self.clear_threshold is not None else base_clear
        return rise, clear


@dataclass(frozen=True)
class Rules:
    segment_gap: timedelta = timedelta(minutes=15)
    rise_min_count: int = 3
    rise_min_span: timedelta = timedelta(minutes=10)
    clear_min_count: int = 2
    clear_min_span: timedelta = timedelta(minutes=5)


@dataclass
class Event:
    type: str  # "warning" | "cleared"
    at: datetime
    reading_id: int
    run_start: datetime
    run_count: int
    span_seconds: float
    # 触发读数当时命中的阈值（跨版本连续读数分别按各自阈值判断）
    rise_threshold: Optional[float] = None
    clear_threshold: Optional[float] = None
    threshold_version_id: Optional[int] = None


@dataclass
class AdjudicationResult:
    status: str  # "normal" | "warning"
    events: List[Event] = field(default_factory=list)
    segments: List[dict] = field(default_factory=list)
    readings_considered: int = 0
    active_warning_since: Optional[datetime] = None


def adjudicate(
    readings: List[Reading],
    rise_threshold: float,
    clear_threshold: float,
    rules: Rules = Rules(),
) -> AdjudicationResult:
    """对单个监测站的有效读数做裁定，返回预警/解除事件时间线。"""
    ordered = sorted(readings, key=lambda r: (r.observed_at, r.id))

    result = AdjudicationResult(status="normal", readings_considered=len(ordered))
    status = "normal"
    rise_run: List[Reading] = []   # 同段内连续达到上升阈值的读数
    clear_run: List[Reading] = []  # 预警后连续不高于解除阈值的读数
    prev: Optional[Reading] = None
    seg_start: Optional[Reading] = None
    seg_count = 0

    def close_segment(last: Reading) -> None:
        assert seg_start is not None
        result.segments.append(
            {
                "start": seg_start.observed_at,
                "end": last.observed_at,
                "count": seg_count,
            }
        )

    for r in ordered:
        # 每条有效读数按观测时间采用最近已生效版本（同一时刻采用新版本）；
        # 读数未携带版本阈值时回退到站点登记基准值。
        r_rise, r_clear = r.thresholds(rise_threshold, clear_threshold)

        if prev is None or (r.observed_at - prev.observed_at) > rules.segment_gap:
            if prev is not None:
                close_segment(prev)
            seg_start = r
            seg_count = 0
            # 断段：上升计数必须同段连续，故重置；
            # 解除计数不清零 —— 断段本身不自动解除预警，后续低水位读数继续累计。
            rise_run = []
        seg_count += 1

        if status == "normal":
            if r.water_level >= r_rise:
                rise_run.append(r)
                span = r.observed_at - rise_run[0].observed_at
                if len(rise_run) >= rules.rise_min_count and span >= rules.rise_min_span:
                    result.events.append(
                        Event(
                            type="warning",
                            at=r.observed_at,
                            reading_id=r.id,
                            run_start=rise_run[0].observed_at,
                            run_count=len(rise_run),
                            span_seconds=span.total_seconds(),
                            rise_threshold=r_rise,
                            clear_threshold=r_clear,
                            threshold_version_id=r.threshold_version_id,
                        )
                    )
                    status = "warning"
                    rise_run = []
                    clear_run = []
            else:
                rise_run = []
        else:  # warning
            if r.water_level <= r_clear:
                clear_run.append(r)
                span = r.observed_at - clear_run[0].observed_at
                if len(clear_run) >= rules.clear_min_count and span >= rules.clear_min_span:
                    result.events.append(
                        Event(
                            type="cleared",
                            at=r.observed_at,
                            reading_id=r.id,
                            run_start=clear_run[0].observed_at,
                            run_count=len(clear_run),
                            span_seconds=span.total_seconds(),
                            rise_threshold=r_rise,
                            clear_threshold=r_clear,
                            threshold_version_id=r.threshold_version_id,
                        )
                    )
                    status = "normal"
                    clear_run = []
                    rise_run = []
            else:
                clear_run = []

        prev = r

    if prev is not None:
        close_segment(prev)

    result.status = status
    if status == "warning":
        for e in reversed(result.events):
            if e.type == "warning":
                result.active_warning_since = e.at
                break
    return result


# ---------------------------------------------------------------- 跨站联动（责任区）

@dataclass(frozen=True)
class StationStateChange:
    """一个监测站在某观测时刻的状态变化（取自该站待发布时间线的预警/解除事件）。"""

    station_id: int
    at: datetime       # 已归一化为带时区时间（UTC）
    state: str         # 变化后的站状态："warning" | "normal"
    order: int = 0     # 同时刻多事件时的稳定次序（读数 id）


@dataclass(frozen=True)
class ZoneEvent:
    """责任区区域预警 / 解除事件。"""

    type: str          # "warning" | "cleared"
    at: datetime
    warning_station_ids: tuple  # 事件发生（同刻合并后）仍在预警的成员站 ID（升序）
    warning_station_count: int


@dataclass
class ZoneReplayResult:
    status: str        # "normal" | "warning"（截至最后一条变化后的区域状态）
    events: List[ZoneEvent] = field(default_factory=list)
    active_warning_since: Optional[datetime] = None


def replay_zone(
    station_changes: List[StationStateChange],
    initial_warning_station_ids,
    trigger_count: int,
    effective_at: datetime,
) -> ZoneReplayResult:
    """按观测时间重放成员站状态变化，裁定责任区区域预警 / 解除。

    规则：
    - initial_warning_station_ids：生效时刻**之前**仍在预警的站，作为初始状态；
    - 从生效时刻起，按观测时间合并所有站在同一时刻的状态变化后再计数；
    - 预警站数达到 K → 发出区域预警；降到 K 以下 → 解除；每个时刻最多一个区域事件；
    - 初始满足 K 时，区域在生效时刻即预警（事件 at = effective_at）。
    """
    warning_set = set(initial_warning_station_ids)
    result = ZoneReplayResult(status="normal")

    # 仅重放生效时刻及之后的变化；同刻合并后统一计数
    from_effective = sorted(
        (c for c in station_changes if c.at >= effective_at),
        key=lambda c: (c.at, c.station_id, c.order),
    )

    ticks: List[tuple[datetime, List[StationStateChange]]] = []
    for c in from_effective:
        if ticks and ticks[-1][0] == c.at:
            ticks[-1][1].append(c)
        else:
            ticks.append((c.at, [c]))

    # 生效时刻的初始判定（此前仍在预警的站计入；初始满足 K 即在生效时刻预警）
    if not ticks or ticks[0][0] != effective_at:
        ticks.insert(0, (effective_at, []))

    zone_status = "normal"
    for at, changes in ticks:
        for ch in changes:
            if ch.state == "warning":
                warning_set.add(ch.station_id)
            else:
                warning_set.discard(ch.station_id)
        count = len(warning_set)
        if zone_status == "normal" and count >= trigger_count:
            zone_status = "warning"
            ids = tuple(sorted(warning_set))
            result.events.append(
                ZoneEvent("warning", at, ids, count)
            )
        elif zone_status == "warning" and count < trigger_count:
            zone_status = "normal"
            ids = tuple(sorted(warning_set))
            result.events.append(
                ZoneEvent("cleared", at, ids, count)
            )

    result.status = zone_status
    if zone_status == "warning":
        for e in reversed(result.events):
            if e.type == "warning":
                result.active_warning_since = e.at
                break
    return result
