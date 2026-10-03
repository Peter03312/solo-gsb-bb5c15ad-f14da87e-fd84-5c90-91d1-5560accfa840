"""山洪水位预警裁定服务 —— FastAPI 入口。

运行：uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import hashlib
import json
import math
import secrets
import sqlite3
from bisect import bisect_right
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .adjudication import Reading, Rules, StationStateChange, adjudicate, replay_zone
from .config import Settings, load_settings
from .db import Database
from .schemas import (
    CorrectRequest,
    DeviceCreate,
    PublishConfirmRequest,
    PublishRequest,
    ReadingIngest,
    StationCreate,
    ThresholdVersionCreate,
    VoidRequest,
    ZoneCreate,
)

# 预演（不落库）时候选阈值版本在时间线中的临时 id：正式登记后会换成真实版本 id。
# 取负值以与任何已登记版本（正整数 id）及基准值（null）区分。
CANDIDATE_VERSION_ID = -1


# ---------------------------------------------------------------- 工具函数

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_stored(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def reading_to_dict(row: sqlite3.Row, device_no: str) -> dict[str, Any]:
    return {
        "id": row["id"],
        "device_no": device_no,
        "seq": row["seq"],
        "observed_at": iso_z(parse_stored(row["observed_at"])),
        "water_level": row["water_level"],
        "original_observed_at": iso_z(parse_stored(row["original_observed_at"])),
        "original_water_level": row["original_water_level"],
        "status": row["status"],
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def revision_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    def ts(v: Optional[str]) -> Optional[str]:
        return iso_z(parse_stored(v)) if v is not None else None

    return {
        "revision_no": row["revision_no"],
        "action": row["action"],
        "old_observed_at": ts(row["old_observed_at"]),
        "old_water_level": row["old_water_level"],
        "new_observed_at": ts(row["new_observed_at"]),
        "new_water_level": row["new_water_level"],
        "reason": row["reason"],
        "auditor": row["auditor"],
        "created_at": row["created_at"],
    }


def _safe_json(obj: Any) -> Any:
    """保证可 JSON 序列化：NaN/Inf 转字符串，未知类型经 default=str 兜底。"""
    def walk(o: Any) -> Any:
        if isinstance(o, float) and not math.isfinite(o):
            return repr(o)
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [walk(v) for v in o]
        return o

    return json.loads(json.dumps(walk(obj), default=str))


# ---------------------------------------------------------------- 受控发布（核对 → 确认）

def canonical_digest(payload: Any) -> str:
    """待发布结果的规范化 SHA-256 摘要：规范化 JSON（分隔符固定、UTF-8、排序键）。

    核对时与确认时用同一套时间线构造函数，产生的 dict 结构稳定，
    因此确认时重算的摘要与核对摘要一致 ⇔ 待发布结果未发生任何变化。
    """
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _events_with_occurrence(events: list[dict[str, Any]]) -> list[tuple[dict[str, Any], int]]:
    """为事件列表编号出现次数：按时间顺序，同对象内同类型事件的第 n 次出现（1 起）。"""
    counts: Counter[str] = Counter()
    out: list[tuple[dict[str, Any], int]] = []
    for ev in events:
        counts[ev["type"]] += 1
        out.append((ev, counts[ev["type"]]))
    return out


def _event_key(ev: dict[str, Any], occurrence: int) -> tuple[str, str, int]:
    """事件身份：（事件类型, 发生时刻, 出现次数）。对象身份在分组外另行携带。"""
    return ev["type"], ev["at"], occurrence


def _event_diff(
    current_events: list[dict[str, Any]],
    baseline_events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按 (类型, 发生时刻, 出现次数) 的多重集合比较，返回 (新增, 消失) 事件。

    同一时刻可合法发生多条同类型事件（反复预警/解除），故以
    “该对象内该类型事件按时间顺序的第 n 次出现”作为出现次数参与匹配。
    """
    cur = Counter(_event_key(ev, occ) for ev, occ in _events_with_occurrence(current_events))
    base = Counter(_event_key(ev, occ) for ev, occ in _events_with_occurrence(baseline_events))
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for key, n in sorted((cur - base).items()):
        for _ in range(n):
            added.append({"type": key[0], "at": key[1], "occurrence": key[2]})
    for key, n in sorted((base - cur).items()):
        for _ in range(n):
            removed.append({"type": key[0], "at": key[1], "occurrence": key[2]})
    return added, removed


# ---------------------------------------------------------------- 应用工厂

def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or load_settings()
    db = Database(settings.db_path)
    rules = Rules(
        segment_gap=timedelta(minutes=settings.segment_gap_minutes),
        rise_min_count=settings.rise_min_count,
        rise_min_span=timedelta(minutes=settings.rise_min_span_minutes),
        clear_min_count=settings.clear_min_count,
        clear_min_span=timedelta(minutes=settings.clear_min_span_minutes),
    )

    app = FastAPI(
        title="山洪水位预警裁定服务",
        version="1.4.0",
        description="监测站/设备登记、读数补传（幂等）、审核修订、阈值版本（含不落库预演）、单站预警裁定、防汛责任区跨站联动，以及受控发布（核对凭证 → 确认冻结）。",
    )
    app.state.db = db
    app.state.settings = settings

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        # 非法时间 / 水位 / 阈值等一律返回 400
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": "请求参数非法", "errors": _safe_json(exc.errors())},
        )

    # ------------------------------------------------------------ 裁定计算

    def threshold_version_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "station_id": row["station_id"],
            "effective_at": iso_z(parse_stored(row["effective_at"])),
            "rise_threshold": row["rise_threshold"],
            "clear_threshold": row["clear_threshold"],
            "reason": row["reason"],
            "created_at": row["created_at"],
        }

    def load_threshold_versions(conn: sqlite3.Connection, station_id: int) -> list[sqlite3.Row]:
        """按生效时刻升序返回某站全部阈值版本（effective_at 为归一化 UTC 字符串）。"""
        return conn.execute(
            """
            SELECT * FROM threshold_versions
            WHERE station_id=?
            ORDER BY effective_at, id
            """,
            (station_id,),
        ).fetchall()

    def resolve_threshold(
        versions: list[sqlite3.Row], obs: datetime
    ) -> tuple[Optional[float], Optional[float], Optional[int]]:
        """按观测时间取最近已生效版本：effective_at <= obs 中最晚者；
        同一时刻采用新版本（bisect_right）。未命中任何版本时返回全 None，
        由调用方回填站点基准阈值。
        """
        effective_times = [parse_stored(v["effective_at"]) for v in versions]
        idx = bisect_right(effective_times, obs) - 1
        if idx < 0:
            return None, None, None
        v = versions[idx]
        return v["rise_threshold"], v["clear_threshold"], v["id"]

    def candidate_version_row(
        station_id: int, effective_at: datetime, rise: float, clear: float, reason: str
    ) -> dict[str, Any]:
        """构造一条**不落库**的候选阈值版本行（预演专用）。

        字段形状对齐 threshold_versions 表行，可直接被 resolve_threshold 使用；
        id 为负哨兵值，裁定时间线中据此标识“预演候选版本”。
        """
        return {
            "id": CANDIDATE_VERSION_ID,
            "station_id": station_id,
            "effective_at": effective_at.isoformat(),
            "rise_threshold": rise,
            "clear_threshold": clear,
            "reason": reason,
            "created_at": None,
        }

    def compute_timeline(
        conn: sqlite3.Connection,
        station_id: int,
        extra_versions: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """裁定单站待发布时间线。

        extra_versions 给出不落库的候选版本（仅预演使用）：与既有版本合并后，
        各读数仍按“观测时间取最近已生效版本、同刻采用新版本”的同一规则命中阈值。
        """
        st = conn.execute("SELECT * FROM stations WHERE id=?", (station_id,)).fetchone()
        if st is None:
            raise HTTPException(status_code=404, detail=f"监测站 {station_id} 不存在")
        versions: list[Any] = list(load_threshold_versions(conn, station_id))
        if extra_versions:
            # 合并既有版本与候选版本后按生效时刻（再按 id）排序；
            # 同刻时候选版本（id=-1）排在既有版本之前，但同刻冲突应由调用方预先拒绝，
            # 正常预演路径下同刻二者不会同时出现。
            versions = versions + [v for v in extra_versions if v["station_id"] == station_id]
            versions.sort(key=lambda v: (parse_stored(v["effective_at"]), v["id"]))
        rows = conn.execute(
            """
            SELECT r.id, r.observed_at, r.water_level
            FROM readings r
            JOIN devices d ON d.id = r.device_id
            WHERE d.station_id = ? AND r.status = 'active'
            ORDER BY r.observed_at, r.id
            """,
            (station_id,),
        ).fetchall()
        readings: list[Reading] = []
        applied: list[dict[str, Any]] = []
        for r in rows:
            obs = parse_stored(r["observed_at"])
            rise, clear, version_id = resolve_threshold(versions, obs)
            if version_id is None:
                # 未命中任何已生效版本 → 采用站点登记基准阈值
                rise, clear = st["rise_threshold"], st["clear_threshold"]
            readings.append(
                Reading(
                    id=r["id"],
                    observed_at=obs,
                    water_level=r["water_level"],
                    rise_threshold=rise,
                    clear_threshold=clear,
                    threshold_version_id=version_id,
                )
            )
            applied.append(
                {
                    "reading_id": r["id"],
                    "observed_at": iso_z(obs),
                    "water_level": r["water_level"],
                    "rise_threshold": rise,
                    "clear_threshold": clear,
                    "threshold_version_id": version_id,  # null 表示使用登记基准值
                }
            )
        res = adjudicate(readings, st["rise_threshold"], st["clear_threshold"], rules)
        return {
            "station_id": station_id,
            "station_name": st["name"],
            # 兼容字段：站点登记基准阈值（未命中任何版本时的基准值）
            "rise_threshold": st["rise_threshold"],
            "clear_threshold": st["clear_threshold"],
            "status": res.status,
            "active_warning_since": iso_z(res.active_warning_since) if res.active_warning_since else None,
            "readings_considered": res.readings_considered,
            "events": [
                {
                    "type": e.type,
                    "at": iso_z(e.at),
                    "reading_id": e.reading_id,
                    "run_start": iso_z(e.run_start),
                    "run_count": e.run_count,
                    "span_seconds": e.span_seconds,
                    # 触发读数当时命中的阈值版本（null 为基准值）
                    "rise_threshold": e.rise_threshold if e.rise_threshold is not None else st["rise_threshold"],
                    "clear_threshold": e.clear_threshold if e.clear_threshold is not None else st["clear_threshold"],
                    "threshold_version_id": e.threshold_version_id,
                }
                for e in res.events
            ],
            "segments": [
                {"start": iso_z(s["start"]), "end": iso_z(s["end"]), "count": s["count"]}
                for s in res.segments
            ],
            # 每条有效读数实际采用的阈值版本（新增字段，老客户端可忽略）
            "readings": applied,
        }

    def get_station_or_404(conn: sqlite3.Connection, station_id: int) -> sqlite3.Row:
        st = conn.execute("SELECT * FROM stations WHERE id=?", (station_id,)).fetchone()
        if st is None:
            raise HTTPException(status_code=404, detail=f"监测站 {station_id} 不存在")
        return st

    # ------------------------------------------------------------ 责任区联动计算

    def get_zone_members(conn: sqlite3.Connection, zone_id: int) -> list[sqlite3.Row]:
        """按登记顺序返回责任区成员站。"""
        return conn.execute(
            """
            SELECT s.id AS id, s.name AS name, s.rise_threshold AS rise_threshold,
                   s.clear_threshold AS clear_threshold, zm.position AS position
            FROM zone_members zm JOIN stations s ON s.id = zm.station_id
            WHERE zm.zone_id=?
            ORDER BY zm.position, s.id
            """,
            (zone_id,),
        ).fetchall()

    def zone_config_dict(zone: sqlite3.Row, members: list[sqlite3.Row]) -> dict[str, Any]:
        return {
            "id": zone["id"],
            "name": zone["name"],
            "effective_at": iso_z(parse_stored(zone["effective_at"])),
            "trigger_count": zone["trigger_count"],
            "created_at": zone["created_at"],
            "members": [
                {
                    "station_id": m["id"],
                    "station_name": m["name"],
                    "position": m["position"],
                }
                for m in members
            ],
            "station_ids": [m["id"] for m in members],
        }

    def compute_zone_timeline(
        conn: sqlite3.Connection,
        zone_id: int,
        threshold_overrides: Optional[dict[int, list[dict[str, Any]]]] = None,
    ) -> dict[str, Any]:
        """重放责任区成员站待发布事件，得到区域联动状态与事件时间线。

        - 成员站待发布事件由各站当前全部有效读数实时裁定（补传 / 修订 / 阈值调整后即时反映）；
        - threshold_overrides：{station_id: [不落库候选版本]}（仅预演使用）——
          目标站按候选版本预演，其他成员站沿用现有待发布结果参与联动；
        - 生效时刻之前仍在预警的站计入初始状态，初始满足 K 即在生效时刻预警；
        - 同一时刻先合并所有站变化，再按 K 判定，每个时刻最多一个区域事件。
        """
        zone = conn.execute("SELECT * FROM zones WHERE id=?", (zone_id,)).fetchone()
        if zone is None:
            raise HTTPException(status_code=404, detail=f"责任区 {zone_id} 不存在")
        members = get_zone_members(conn, zone_id)
        effective_at = parse_stored(zone["effective_at"])
        trigger_count = zone["trigger_count"]

        overrides = threshold_overrides or {}

        changes: list[StationStateChange] = []
        initial_warning: list[int] = []
        member_status: list[dict[str, Any]] = []
        for m in members:
            station_id = m["id"]
            # 同一请求内同一站只裁定一次（预演与正式路径共用同一时间线结果）
            tl = compute_timeline(conn, station_id, overrides.get(station_id))
            last_before: Optional[str] = None
            for ev in tl["events"]:
                ev_at = parse_stored(ev["at"])
                if ev_at >= effective_at:
                    changes.append(
                        StationStateChange(
                            station_id=station_id,
                            at=ev_at,
                            state="warning" if ev["type"] == "warning" else "normal",
                            order=ev["reading_id"],
                        )
                    )
                else:
                    last_before = ev["type"]
            # 生效之前最后一个事件为预警（且其后没有解除）→ 生效时仍在预警
            if last_before == "warning":
                initial_warning.append(station_id)
            member_status.append(
                {
                    "station_id": station_id,
                    "station_name": m["name"],
                    # 该站截至当前（待发布）是否仍在预警
                    "warning": tl["status"] == "warning",
                    "warning_before_effective": last_before == "warning",
                }
            )

        replay = replay_zone(changes, initial_warning, trigger_count, effective_at)
        return {
            "zone_id": zone_id,
            "zone_name": zone["name"],
            "status": replay.status,
            "active_warning_since": (
                iso_z(replay.active_warning_since) if replay.active_warning_since else None
            ),
            "trigger_count": trigger_count,
            "warning_station_count": sum(1 for ms in member_status if ms["warning"]),
            "warning_station_ids": sorted(
                ms["station_id"] for ms in member_status if ms["warning"]
            ),
            "events": [
                {
                    "type": e.type,
                    "at": iso_z(e.at),
                    "warning_station_ids": list(e.warning_station_ids),
                    "warning_station_count": e.warning_station_count,
                }
                for e in replay.events
            ],
            "members": member_status,
        }

    def get_reading_or_404(conn: sqlite3.Connection, reading_id: int) -> tuple[sqlite3.Row, str]:
        row = conn.execute(
            """
            SELECT r.*, d.device_no AS device_no
            FROM readings r JOIN devices d ON d.id = r.device_id
            WHERE r.id = ?
            """,
            (reading_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"读数 {reading_id} 不存在")
        return row, row["device_no"]

    def check_water_level(level: float) -> None:
        if not (settings.min_water_level <= level <= settings.max_water_level):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"水位 {level} 超出合法范围 "
                    f"[{settings.min_water_level}, {settings.max_water_level}]"
                ),
            )

    # ------------------------------------------------------------ 监测站 / 设备

    @app.post("/stations", status_code=201, tags=["监测站"])
    def create_station(body: StationCreate) -> dict[str, Any]:
        now = iso_z(utc_now())
        with db.write_tx() as conn:
            cur = conn.execute(
                "INSERT INTO stations(name, rise_threshold, clear_threshold, created_at) VALUES (?,?,?,?)",
                (body.name, body.rise_threshold, body.clear_threshold, now),
            )
            row = conn.execute("SELECT * FROM stations WHERE id=?", (cur.lastrowid,)).fetchone()
            bump_data_version(conn)
        return {
            "id": row["id"],
            "name": row["name"],
            "rise_threshold": row["rise_threshold"],
            "clear_threshold": row["clear_threshold"],
            "created_at": row["created_at"],
        }

    @app.get("/stations", tags=["监测站"])
    def list_stations() -> list[dict[str, Any]]:
        with db.read() as conn:
            rows = conn.execute("SELECT * FROM stations ORDER BY id").fetchall()
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "rise_threshold": r["rise_threshold"],
                "clear_threshold": r["clear_threshold"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    @app.get("/stations/{station_id}", tags=["监测站"])
    def get_station(station_id: int) -> dict[str, Any]:
        with db.read() as conn:
            st = get_station_or_404(conn, station_id)
            devices = conn.execute(
                "SELECT * FROM devices WHERE station_id=? ORDER BY id", (station_id,)
            ).fetchall()
        return {
            "id": st["id"],
            "name": st["name"],
            "rise_threshold": st["rise_threshold"],
            "clear_threshold": st["clear_threshold"],
            "created_at": st["created_at"],
            "devices": [
                {"id": d["id"], "device_no": d["device_no"], "created_at": d["created_at"]}
                for d in devices
            ],
        }

    @app.post("/stations/{station_id}/devices", status_code=201, tags=["设备"])
    def create_device(station_id: int, body: DeviceCreate) -> dict[str, Any]:
        now = iso_z(utc_now())
        with db.write_tx() as conn:
            get_station_or_404(conn, station_id)
            try:
                cur = conn.execute(
                    "INSERT INTO devices(device_no, station_id, created_at) VALUES (?,?,?)",
                    (body.device_no, station_id, now),
                )
            except sqlite3.IntegrityError:
                raise HTTPException(status_code=409, detail=f"设备号 {body.device_no} 已存在")
            row = conn.execute("SELECT * FROM devices WHERE id=?", (cur.lastrowid,)).fetchone()
            bump_data_version(conn)
        return {
            "id": row["id"],
            "device_no": row["device_no"],
            "station_id": row["station_id"],
            "created_at": row["created_at"],
        }

    @app.get("/stations/{station_id}/devices", tags=["设备"])
    def list_devices(station_id: int) -> list[dict[str, Any]]:
        with db.read() as conn:
            get_station_or_404(conn, station_id)
            rows = conn.execute(
                "SELECT * FROM devices WHERE station_id=? ORDER BY id", (station_id,)
            ).fetchall()
        return [
            {"id": r["id"], "device_no": r["device_no"], "station_id": r["station_id"], "created_at": r["created_at"]}
            for r in rows
        ]

    # ------------------------------------------------------------ 读数补传（幂等）

    @app.post("/readings", tags=["读数"])
    def ingest_reading(body: ReadingIngest, response: Response) -> dict[str, Any]:
        check_water_level(body.water_level)
        obs_utc = body.observed_at.astimezone(timezone.utc)
        obs_str = obs_utc.isoformat()
        now = iso_z(utc_now())

        with db.write_tx() as conn:
            dev = conn.execute("SELECT * FROM devices WHERE device_no=?", (body.device_no,)).fetchone()
            if dev is None:
                raise HTTPException(status_code=404, detail=f"未知设备号 {body.device_no}")

            existing = conn.execute(
                "SELECT * FROM readings WHERE device_id=? AND seq=?", (dev["id"], body.seq)
            ).fetchone()
            if existing is not None:
                same = (
                    parse_stored(existing["observed_at"]) == obs_utc
                    and existing["water_level"] == body.water_level
                )
                if same:
                    # 同号同序号同内容：返回原结果
                    response.status_code = status.HTTP_200_OK
                    return {**reading_to_dict(existing, dev["device_no"]), "deduplicated": True}
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": f"设备 {body.device_no} 序号 {body.seq} 已存在且内容不一致",
                        "existing": reading_to_dict(existing, dev["device_no"]),
                        "submitted": {
                            "observed_at": iso_z(obs_utc),
                            "water_level": body.water_level,
                        },
                    },
                )
            try:
                cur = conn.execute(
                    """
                    INSERT INTO readings(device_id, seq, observed_at, water_level,
                                         original_observed_at, original_water_level,
                                         status, version, created_at, updated_at)
                    VALUES (?,?,?,?,?,?, 'active', 1, ?, ?)
                    """,
                    (dev["id"], body.seq, obs_str, body.water_level, obs_str, body.water_level, now, now),
                )
            except sqlite3.IntegrityError:
                # 并发下唯一约束兜底：按已存在处理，重新读取后比较
                existing = conn.execute(
                    "SELECT * FROM readings WHERE device_id=? AND seq=?", (dev["id"], body.seq)
                ).fetchone()
                if existing is not None and parse_stored(existing["observed_at"]) == obs_utc \
                        and existing["water_level"] == body.water_level:
                    response.status_code = status.HTTP_200_OK
                    return {**reading_to_dict(existing, dev["device_no"]), "deduplicated": True}
                raise HTTPException(status_code=409, detail=f"设备 {body.device_no} 序号 {body.seq} 冲突")
            row = conn.execute("SELECT * FROM readings WHERE id=?", (cur.lastrowid,)).fetchone()
            # 仅实际新增读数才推进版本（幂等重传不改数据，不影响待确认凭证）
            bump_data_version(conn)

        response.status_code = status.HTTP_201_CREATED
        return {**reading_to_dict(row, dev["device_no"]), "deduplicated": False}

    @app.get("/stations/{station_id}/readings", tags=["读数"])
    def list_station_readings(station_id: int, include_voided: bool = False) -> list[dict[str, Any]]:
        with db.read() as conn:
            get_station_or_404(conn, station_id)
            sql = """
                SELECT r.*, d.device_no AS device_no
                FROM readings r JOIN devices d ON d.id = r.device_id
                WHERE d.station_id = ?
            """
            if not include_voided:
                sql += " AND r.status = 'active'"
            sql += " ORDER BY r.observed_at, r.id"
            rows = conn.execute(sql, (station_id,)).fetchall()
        return [reading_to_dict(r, r["device_no"]) for r in rows]

    @app.get("/readings/{reading_id}", tags=["读数"])
    def get_reading(reading_id: int) -> dict[str, Any]:
        with db.read() as conn:
            row, device_no = get_reading_or_404(conn, reading_id)
        return reading_to_dict(row, device_no)

    # ------------------------------------------------------------ 审核：作废 / 更正 / 修订历史

    @app.post("/readings/{reading_id}/void", tags=["审核"])
    def void_reading(reading_id: int, body: VoidRequest) -> dict[str, Any]:
        now = iso_z(utc_now())
        with db.write_tx() as conn:
            row, device_no = get_reading_or_404(conn, reading_id)
            if row["status"] == "voided":
                raise HTTPException(status_code=409, detail=f"读数 {reading_id} 已作废")
            conn.execute(
                """
                INSERT INTO reading_revisions(reading_id, revision_no, action,
                                              old_observed_at, old_water_level,
                                              new_observed_at, new_water_level,
                                              reason, auditor, created_at)
                VALUES (?,?,?,?,?, NULL, NULL, ?,?,?)
                """,
                (
                    reading_id, row["version"], "void",
                    row["observed_at"], row["water_level"],
                    body.reason, body.auditor, now,
                ),
            )
            conn.execute(
                "UPDATE readings SET status='voided', version=version+1, updated_at=? WHERE id=?",
                (now, reading_id),
            )
            updated, _ = get_reading_or_404(conn, reading_id)
            station_id = conn.execute(
                "SELECT station_id FROM devices WHERE id=?", (updated["device_id"],)
            ).fetchone()["station_id"]
            timeline = compute_timeline(conn, station_id)
            bump_data_version(conn)
        return {
            "reading": reading_to_dict(updated, device_no),
            "pending_adjudication": {"station_id": station_id, "status": timeline["status"], "events": timeline["events"]},
        }

    @app.post("/readings/{reading_id}/correct", tags=["审核"])
    def correct_reading(reading_id: int, body: CorrectRequest) -> dict[str, Any]:
        if body.water_level is not None:
            check_water_level(body.water_level)
        now = iso_z(utc_now())
        with db.write_tx() as conn:
            row, device_no = get_reading_or_404(conn, reading_id)
            if row["status"] == "voided":
                raise HTTPException(status_code=409, detail=f"读数 {reading_id} 已作废，不能更正")
            new_obs = (
                body.observed_at.astimezone(timezone.utc).isoformat()
                if body.observed_at is not None
                else row["observed_at"]
            )
            new_level = body.water_level if body.water_level is not None else row["water_level"]
            conn.execute(
                """
                INSERT INTO reading_revisions(reading_id, revision_no, action,
                                              old_observed_at, old_water_level,
                                              new_observed_at, new_water_level,
                                              reason, auditor, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    reading_id, row["version"], "correct",
                    row["observed_at"], row["water_level"],
                    new_obs, new_level,
                    body.reason, body.auditor, now,
                ),
            )
            conn.execute(
                "UPDATE readings SET observed_at=?, water_level=?, version=version+1, updated_at=? WHERE id=?",
                (new_obs, new_level, now, reading_id),
            )
            updated, _ = get_reading_or_404(conn, reading_id)
            station_id = conn.execute(
                "SELECT station_id FROM devices WHERE id=?", (updated["device_id"],)
            ).fetchone()["station_id"]
            timeline = compute_timeline(conn, station_id)
            bump_data_version(conn)
        return {
            "reading": reading_to_dict(updated, device_no),
            "pending_adjudication": {"station_id": station_id, "status": timeline["status"], "events": timeline["events"]},
        }

    @app.get("/readings/{reading_id}/revisions", tags=["审核"])
    def list_revisions(reading_id: int) -> dict[str, Any]:
        with db.read() as conn:
            row, device_no = get_reading_or_404(conn, reading_id)
            revs = conn.execute(
                "SELECT * FROM reading_revisions WHERE reading_id=? ORDER BY revision_no",
                (reading_id,),
            ).fetchall()
        return {
            "reading": reading_to_dict(row, device_no),
            "original": {
                "observed_at": iso_z(parse_stored(row["original_observed_at"])),
                "water_level": row["original_water_level"],
            },
            "revisions": [revision_to_dict(r) for r in revs],
        }

    # ------------------------------------------------------------ 阈值版本（防汛方案调整）

    def find_duplicate_threshold_version(
        conn: sqlite3.Connection, station_id: int, eff_utc: datetime
    ) -> Optional[sqlite3.Row]:
        """同站同一生效瞬间（UTC 归一化比较，含不同时区表示）的既有版本。"""
        return conn.execute(
            "SELECT id FROM threshold_versions WHERE station_id=? AND effective_at=?",
            (station_id, eff_utc.isoformat()),
        ).fetchone()

    def duplicate_version_error(station_id: int, eff_utc: datetime, version_id: int) -> HTTPException:
        return HTTPException(
            status_code=409,
            detail={
                "message": f"监测站 {station_id} 已存在生效时间 {iso_z(eff_utc)} 的阈值版本",
                "existing_version_id": version_id,
            },
        )

    @app.post("/stations/{station_id}/threshold-versions", status_code=201, tags=["阈值版本"])
    def create_threshold_version(station_id: int, body: ThresholdVersionCreate) -> dict[str, Any]:
        """登记一版自生效时间起采用的阈值。

        - 生效时间必须带时区；同站重复生效时间（同一时刻，含不同时区表示）→ 409；
        - 阈值须为有限数值且上升阈值严格大于解除阈值，否则 400；
        - 站点不存在 → 404；
        - 阈值调整本身不产生预警或解除事件，仅影响之后的裁定计算。
        """
        eff_utc = body.effective_at.astimezone(timezone.utc)
        eff_str = eff_utc.isoformat()
        now = iso_z(utc_now())
        with db.write_tx() as conn:
            get_station_or_404(conn, station_id)
            dup = find_duplicate_threshold_version(conn, station_id, eff_utc)
            if dup is not None:
                raise duplicate_version_error(station_id, eff_utc, dup["id"])
            try:
                cur = conn.execute(
                    """
                    INSERT INTO threshold_versions(station_id, effective_at,
                                                   rise_threshold, clear_threshold,
                                                   reason, created_at)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (station_id, eff_str, body.rise_threshold, body.clear_threshold, body.reason, now),
                )
            except sqlite3.IntegrityError:
                # 并发下唯一约束兜底
                raise HTTPException(
                    status_code=409,
                    detail=f"监测站 {station_id} 已存在生效时间 {iso_z(eff_utc)} 的阈值版本",
                )
            row = conn.execute(
                "SELECT * FROM threshold_versions WHERE id=?", (cur.lastrowid,)
            ).fetchone()
            # 调整本身不产生预警/解除：重算仅用于回显当前待发布状态，不写入任何事件记录
            timeline = compute_timeline(conn, station_id)
            bump_data_version(conn)
        return {
            "version": threshold_version_to_dict(row),
            "pending_adjudication": {
                "station_id": station_id,
                "status": timeline["status"],
                "events": timeline["events"],
            },
        }

    @app.get("/stations/{station_id}/threshold-versions", tags=["阈值版本"])
    def list_threshold_versions(station_id: int) -> dict[str, Any]:
        """查询某站阈值版本历史（按生效时间升序）。

        baseline 为登记时的阈值：任何观测时间早于首个生效版本的读数都以它为基准。
        """
        with db.read() as conn:
            st = get_station_or_404(conn, station_id)
            rows = load_threshold_versions(conn, station_id)
        return {
            "station_id": station_id,
            "baseline": {
                "rise_threshold": st["rise_threshold"],
                "clear_threshold": st["clear_threshold"],
            },
            "versions": [threshold_version_to_dict(r) for r in rows],
        }

    @app.post("/stations/{station_id}/threshold-versions/preview", tags=["阈值版本"])
    def preview_threshold_version(station_id: int, body: ThresholdVersionCreate) -> dict[str, Any]:
        """**不落库预演**一版阈值调整对待发布预警的影响（正式登记前演练）。

        - 入参与正式登记完全一致，沿用同一套校验与冲突规则：
          必带时区、阈值有限且上升严格大于解除、原因必填（非法 → 400）；
          未知站 → 404；既有版本与候选**同一生效瞬间** → 409；
        - 候选版本不写入数据库：不改变待发布查询、不改变已冻结发布、版本历史不可见；
        - 返回目标站与**所有包含它的责任区**的现状（current）/ 预演（preview）状态与
          完整事件时间线；责任区内其他成员站按现有待发布结果参与联动，
          区域同刻变化合并后判定（与正式规则一致）；
        - 整个结果取自同一只读数据视图；无其他写入时，随后正式登记得到的状态、
          事件类型与时刻与本预演一致。
        """
        eff_utc = body.effective_at.astimezone(timezone.utc)
        with db.read() as conn:
            get_station_or_404(conn, station_id)
            # 冲突规则与正式登记一致：同站同一生效瞬间即冲突（不区分时区表示）
            dup = find_duplicate_threshold_version(conn, station_id, eff_utc)
            if dup is not None:
                raise duplicate_version_error(station_id, eff_utc, dup["id"])

            candidate = candidate_version_row(
                station_id, eff_utc, body.rise_threshold, body.clear_threshold, body.reason
            )
            overrides = {station_id: [candidate]}

            # 现状与预演在同一连接（同一数据视图 + 进程内锁）内计算，全程无写入
            current_station = compute_timeline(conn, station_id)
            preview_station = compute_timeline(conn, station_id, [candidate])

            zone_rows = conn.execute(
                """
                SELECT zone_id FROM zone_members WHERE station_id=?
                ORDER BY zone_id
                """,
                (station_id,),
            ).fetchall()
            zones: list[dict[str, Any]] = []
            for zr in zone_rows:
                zone_id = zr["zone_id"]
                current_zone = compute_zone_timeline(conn, zone_id)
                preview_zone = compute_zone_timeline(conn, zone_id, overrides)
                zones.append(
                    {
                        "zone_id": zone_id,
                        "zone_name": current_zone["zone_name"],
                        "trigger_count": current_zone["trigger_count"],
                        "current": {
                            "published": False,
                            "status": current_zone["status"],
                            "active_warning_since": current_zone["active_warning_since"],
                            "warning_station_ids": current_zone["warning_station_ids"],
                            "warning_station_count": current_zone["warning_station_count"],
                            "events": current_zone["events"],
                            "members": current_zone["members"],
                        },
                        "preview": {
                            "published": False,
                            "status": preview_zone["status"],
                            "active_warning_since": preview_zone["active_warning_since"],
                            "warning_station_ids": preview_zone["warning_station_ids"],
                            "warning_station_count": preview_zone["warning_station_count"],
                            "events": preview_zone["events"],
                            # 其他成员站沿用现有待发布结果参与联动（仅目标站发生变化）
                            "members": preview_zone["members"],
                        },
                    }
                )

        return {
            "dry_run": True,
            "station_id": station_id,
            "candidate": {
                "effective_at": iso_z(eff_utc),
                "rise_threshold": body.rise_threshold,
                "clear_threshold": body.clear_threshold,
                "reason": body.reason,
                "threshold_version_id": CANDIDATE_VERSION_ID,
            },
            "current": {"published": False, **current_station},
            "preview": {"published": False, **preview_station},
            "zones": zones,
        }

    # ------------------------------------------------------------ 裁定时间线（待发布）

    @app.get("/stations/{station_id}/timeline", tags=["裁定"])
    def pending_timeline(station_id: int) -> dict[str, Any]:
        with db.read() as conn:
            timeline = compute_timeline(conn, station_id)
        return {"published": False, **timeline}

    # ------------------------------------------------------------ 防汛责任区（跨站联动）

    @app.post("/zones", status_code=201, tags=["责任区"])
    def create_zone(body: ZoneCreate) -> dict[str, Any]:
        """登记防汛责任区。

        - 名称非空、生效时间必带时区、成员站 ID 互不重复、1 ≤ K ≤ 成员数，否则 400；
        - 任一成员站不存在 → 404；
        - 登记后配置不变（不提供修改 / 删除接口），联动结果随后续读数 / 修订 / 阈值调整实时重算。
        """
        eff_utc = body.effective_at.astimezone(timezone.utc)
        eff_str = eff_utc.isoformat()
        now = iso_z(utc_now())
        with db.write_tx() as conn:
            unknown = [
                sid for sid in body.station_ids
                if conn.execute("SELECT 1 FROM stations WHERE id=?", (sid,)).fetchone() is None
            ]
            if unknown:
                raise HTTPException(
                    status_code=404,
                    detail={"message": "存在未知监测站 ID", "unknown_station_ids": unknown},
                )
            cur = conn.execute(
                "INSERT INTO zones(name, effective_at, trigger_count, created_at) VALUES (?,?,?,?)",
                (body.name, eff_str, body.trigger_count, now),
            )
            zone_id = cur.lastrowid
            for position, station_id in enumerate(body.station_ids):
                conn.execute(
                    "INSERT INTO zone_members(zone_id, station_id, position) VALUES (?,?,?)",
                    (zone_id, station_id, position),
                )
            zone = conn.execute("SELECT * FROM zones WHERE id=?", (zone_id,)).fetchone()
            members = get_zone_members(conn, zone_id)
            timeline = compute_zone_timeline(conn, zone_id)
            bump_data_version(conn)
        config = zone_config_dict(zone, members)
        pending = {
            "zone_id": zone_id,
            "status": timeline["status"],
            "warning_station_ids": timeline["warning_station_ids"],
            "warning_station_count": timeline["warning_station_count"],
            "events": timeline["events"],
        }
        return {**config, "pending_zone": pending}

    @app.get("/zones", tags=["责任区"])
    def list_zones() -> list[dict[str, Any]]:
        with db.read() as conn:
            zones = conn.execute("SELECT * FROM zones ORDER BY id").fetchall()
            result = []
            for z in zones:
                result.append(zone_config_dict(z, get_zone_members(conn, z["id"])))
        return result

    @app.get("/zones/{zone_id}", tags=["责任区"])
    def get_zone(zone_id: int) -> dict[str, Any]:
        with db.read() as conn:
            zone = conn.execute("SELECT * FROM zones WHERE id=?", (zone_id,)).fetchone()
            if zone is None:
                raise HTTPException(status_code=404, detail=f"责任区 {zone_id} 不存在")
            config = zone_config_dict(zone, get_zone_members(conn, zone_id))
        return config

    @app.get("/zones/{zone_id}/timeline", tags=["责任区"])
    def pending_zone_timeline(zone_id: int) -> dict[str, Any]:
        """待发布区域联动结果（随补传 / 审核修订 / 阈值版本调整即时重算）。"""
        with db.read() as conn:
            timeline = compute_zone_timeline(conn, zone_id)
        return {"published": False, **timeline}

    @app.get("/public/zones/{zone_id}/timeline", tags=["公开结果"])
    def public_zone_timeline(zone_id: int) -> dict[str, Any]:
        """某责任区最近一次发布冻结的联动结果；责任区不存在或尚无发布均明确报无结果（404）。"""
        with db.read() as conn:
            zone = conn.execute("SELECT * FROM zones WHERE id=?", (zone_id,)).fetchone()
            if zone is None:
                raise HTTPException(status_code=404, detail=f"责任区 {zone_id} 不存在")
            row = conn.execute(
                """
                SELECT pz.timeline_json, p.id AS pub_id, p.published_at
                FROM publication_zones pz
                JOIN publications p ON p.id = pz.publication_id
                WHERE pz.zone_id=?
                ORDER BY pz.publication_id DESC LIMIT 1
                """,
                (zone_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"责任区 {zone_id} 尚无已发布结果")
        return {
            "publication_id": row["pub_id"],
            "published_at": row["published_at"],
            "published": True,
            **json.loads(row["timeline_json"]),
        }

    # ------------------------------------------------------------ 发布（冻结公开结果）

    def bump_data_version(conn: sqlite3.Connection) -> int:
        """推进数据版本（影响裁定的写入后调用，与写入处于同一事务）。

        版本号持久化、单调递增；受控发布确认据此判断核对后是否发生过写入，
        其他发布先完成也会推进版本，从而使未确认凭证失效。
        """
        conn.execute(
            """
            UPDATE app_meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)
            WHERE key='data_version'
            """
        )
        return current_data_version(conn)

    def current_data_version(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT value FROM app_meta WHERE key='data_version'").fetchone()
        return int(row["value"]) if row is not None else 0

    def build_pending_payload(conn: sqlite3.Connection) -> dict[str, Any]:
        """计算当前全部监测站与责任区的**待发布**结果（核对与确认共用，保证同源）。"""
        station_rows = conn.execute("SELECT id FROM stations ORDER BY id").fetchall()
        stations = [compute_timeline(conn, r["id"]) for r in station_rows]
        zone_rows = conn.execute("SELECT id FROM zones ORDER BY id").fetchall()
        zones = [compute_zone_timeline(conn, r["id"]) for r in zone_rows]
        return {"stations": stations, "zones": zones}

    def latest_publication_row(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM publications ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def load_baseline_payload(conn: sqlite3.Connection, pub_id: int) -> dict[str, Any]:
        """读取某次发布冻结的全部站点/责任区时间线（按 id 索引，便于对比）。"""
        st_rows = conn.execute(
            "SELECT station_id, timeline_json FROM publication_timelines WHERE publication_id=?",
            (pub_id,),
        ).fetchall()
        zone_rows = conn.execute(
            "SELECT zone_id, timeline_json FROM publication_zones WHERE publication_id=?",
            (pub_id,),
        ).fetchall()
        return {
            "stations": {r["station_id"]: json.loads(r["timeline_json"]) for r in st_rows},
            "zones": {r["zone_id"]: json.loads(r["timeline_json"]) for r in zone_rows},
        }

    def build_check_result(
        conn: sqlite3.Connection,
        baseline_pub: Optional[sqlite3.Row],
        pending: dict[str, Any],
    ) -> dict[str, Any]:
        """以待发布结果相对基准快照（首次发布为空快照）做核对，构造核对差异。

        - 各站及责任区给出状态前值/后值；新登记对象按空基准（before=null）比较；
        - 新增/消失事件按 (对象, 事件类型, 发生时刻, 出现次数) 的多重集合比较。
        """
        baseline = load_baseline_payload(conn, baseline_pub["id"]) if baseline_pub is not None else {"stations": {}, "zones": {}}

        station_diffs: list[dict[str, Any]] = []
        events_added: list[dict[str, Any]] = []
        events_removed: list[dict[str, Any]] = []
        for tl in pending["stations"]:
            sid = tl["station_id"]
            base_tl = baseline["stations"].get(sid)
            before = base_tl["status"] if base_tl is not None else None
            after = tl["status"]
            added, removed = _event_diff(tl["events"], base_tl["events"] if base_tl is not None else [])
            station_diffs.append(
                {
                    "station_id": sid,
                    "station_name": tl["station_name"],
                    "status_before": before,
                    "status_after": after,
                    "changed": before != after,
                    "new_object": base_tl is None,
                    "events_added": added,
                    "events_removed": removed,
                }
            )
            for ev in added:
                events_added.append({"object_type": "station", "object_id": sid, **ev})
            for ev in removed:
                events_removed.append({"object_type": "station", "object_id": sid, **ev})

        zone_diffs: list[dict[str, Any]] = []
        for ztl in pending["zones"]:
            zid = ztl["zone_id"]
            base_ztl = baseline["zones"].get(zid)
            before = base_ztl["status"] if base_ztl is not None else None
            after = ztl["status"]
            added, removed = _event_diff(ztl["events"], base_ztl["events"] if base_ztl is not None else [])
            zone_diffs.append(
                {
                    "zone_id": zid,
                    "zone_name": ztl["zone_name"],
                    "status_before": before,
                    "status_after": after,
                    "changed": before != after,
                    "new_object": base_ztl is None,
                    "trigger_count": ztl["trigger_count"],
                    "warning_station_ids_before": (
                        base_ztl["warning_station_ids"] if base_ztl is not None else []
                    ),
                    "warning_station_ids_after": ztl["warning_station_ids"],
                    "events_added": added,
                    "events_removed": removed,
                }
            )
            for ev in added:
                events_added.append({"object_type": "zone", "object_id": zid, **ev})
            for ev in removed:
                events_removed.append({"object_type": "zone", "object_id": zid, **ev})

        def _counts(items: list[dict[str, Any]]) -> dict[str, int]:
            c = Counter((i["object_type"], i["type"]) for i in items)
            return {
                "total": len(items),
                "station_warning": c.get(("station", "warning"), 0),
                "station_cleared": c.get(("station", "cleared"), 0),
                "zone_warning": c.get(("zone", "warning"), 0),
                "zone_cleared": c.get(("zone", "cleared"), 0),
            }

        return {
            "baseline_publication_id": baseline_pub["id"] if baseline_pub is not None else None,
            "baseline_published_at": baseline_pub["published_at"] if baseline_pub is not None else None,
            "stations": station_diffs,
            "zones": zone_diffs,
            "events_added": sorted(
                events_added, key=lambda e: (e["object_type"], e["object_id"], e["at"], e["type"])
            ),
            "events_removed": sorted(
                events_removed, key=lambda e: (e["object_type"], e["object_id"], e["at"], e["type"])
            ),
            "summary": {
                "stations_total": len(station_diffs),
                "stations_status_changed": sum(1 for s in station_diffs if s["changed"]),
                "stations_new": sum(1 for s in station_diffs if s["new_object"]),
                "zones_total": len(zone_diffs),
                "zones_status_changed": sum(1 for z in zone_diffs if z["changed"]),
                "zones_new": sum(1 for z in zone_diffs if z["new_object"]),
                "events_added": _counts(events_added),
                "events_removed": _counts(events_removed),
            },
        }

    def freeze_publication(conn: sqlite3.Connection, note: Optional[str], now: str) -> tuple[int, dict[str, Any], dict[str, Any]]:
        """在当前事务内冻结全部待发布时间线并写入发布记录，返回 (pub_id, 摘要, 待发布负载)。"""
        cur = conn.execute(
            "INSERT INTO publications(published_at, note) VALUES (?,?)", (now, note)
        )
        pub_id = cur.lastrowid
        pending = build_pending_payload(conn)
        summaries: list[dict[str, Any]] = []
        for tl in pending["stations"]:
            conn.execute(
                "INSERT INTO publication_timelines(publication_id, station_id, timeline_json) VALUES (?,?,?)",
                (pub_id, tl["station_id"], json.dumps(tl, ensure_ascii=False)),
            )
            summaries.append(
                {
                    "station_id": tl["station_id"],
                    "station_name": tl["station_name"],
                    "status": tl["status"],
                    "event_count": len(tl["events"]),
                }
            )
        zone_summaries: list[dict[str, Any]] = []
        for ztl in pending["zones"]:
            conn.execute(
                "INSERT INTO publication_zones(publication_id, zone_id, timeline_json) VALUES (?,?,?)",
                (pub_id, ztl["zone_id"], json.dumps(ztl, ensure_ascii=False)),
            )
            zone_summaries.append(
                {
                    "zone_id": ztl["zone_id"],
                    "zone_name": ztl["zone_name"],
                    "status": ztl["status"],
                    "event_count": len(ztl["events"]),
                }
            )
        response = {
            "publication_id": pub_id,
            "published_at": now,
            "note": note,
            "stations": summaries,
            "zones": zone_summaries,
        }
        return pub_id, response, pending

    @app.post("/publish", status_code=201, tags=["发布"])
    def publish(body: Optional[PublishRequest] = None) -> dict[str, Any]:
        note = body.note if body else None
        now = iso_z(utc_now())
        with db.write_tx() as conn:
            _, response, _ = freeze_publication(conn, note, now)
            # 发布完成即推进数据版本：其他待确认凭证因基准已被新发布超越而须拒绝
            bump_data_version(conn)
        return response

    @app.post("/publish/checks", status_code=201, tags=["受控发布"])
    def publish_check() -> dict[str, Any]:
        """值班员发布前核对：对比当前待发布裁定与最新公开快照（首次发布以空快照为基准）。

        返回基准发布编号、各站及责任区状态前后值，以及按
        （对象, 事件类型, 发生时刻, 出现次数）计算的新增/消失事件，
        并附一次性确认凭证 token。核对本身不改变裁定与公开结果。
        """
        now = iso_z(utc_now())
        with db.write_tx() as conn:
            baseline_pub = latest_publication_row(conn)
            pending = build_pending_payload(conn)
            digest = canonical_digest(pending)
            data_version = current_data_version(conn)

            token = secrets.token_urlsafe(32)
            conn.execute(
                """
                INSERT INTO publish_checks(token_hash, publication_id, data_version,
                                           pending_digest, note, used, voided, created_at)
                VALUES (?,?,?,?,?, 0, 0, ?)
                """,
                (
                    hash_token(token),
                    baseline_pub["id"] if baseline_pub is not None else None,
                    data_version,
                    digest,
                    None,
                    now,
                ),
            )
            check = build_check_result(conn, baseline_pub, pending)

        return {
            "checked_at": now,
            "data_version": data_version,
            "token": token,
            **check,
        }

    @app.post("/publish/confirm", status_code=201, tags=["受控发布"])
    def publish_confirm(body: PublishConfirmRequest) -> dict[str, Any]:
        """凭核对凭证完成受控发布（凭证校验与冻结在**同一写事务**内完成）。

        - 未知凭证 → 404；
        - 已使用 / 核对后数据版本被推进（站点、设备、读数补传或修订、阈值版本、
          责任区登记或其他发布先完成等）/ 重算待发布结果与核对时不一致（冻结内容
          必须与核对时一致）→ 409，且不生成发布记录；
        - 凭证只能成功使用一次，成功后返回发布编号。
        """
        now = iso_z(utc_now())
        with db.write_tx() as conn:
            row = conn.execute(
                "SELECT * FROM publish_checks WHERE token_hash=?",
                (hash_token(body.token),),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="无效的发布确认凭证")
            if row["used"]:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "reason": "token_already_used",
                        "message": "确认凭证已使用，凭证只能成功使用一次",
                        "publication_id": row["confirmed_publication_id"],
                    },
                )
            if row["voided"]:
                raise HTTPException(status_code=409, detail={"reason": "token_voided", "message": "确认凭证已作废"})

            latest_version = current_data_version(conn)
            if latest_version != row["data_version"]:
                # 核对后发生过影响裁定的写入或有发布先完成：作废凭证且不生成发布
                conn.execute(
                    "UPDATE publish_checks SET voided=1 WHERE token_hash=?", (row["token_hash"],)
                )
                raise HTTPException(
                    status_code=409,
                    detail={
                        "reason": "data_changed_after_check",
                        "message": "核对后发生影响裁定的写入或其他发布先完成，确认被拒绝",
                        "check_data_version": row["data_version"],
                        "current_data_version": latest_version,
                    },
                )

            pending = build_pending_payload(conn)
            digest = canonical_digest(pending)
            if digest != row["pending_digest"]:
                # 双保险：版本相同但待发布结果与核对时不一致，同样拒绝
                conn.execute(
                    "UPDATE publish_checks SET voided=1 WHERE token_hash=?", (row["token_hash"],)
                )
                raise HTTPException(
                    status_code=409,
                    detail={"reason": "pending_mismatch", "message": "冻结内容与核对时的待发布结果不一致，确认被拒绝"},
                )

            pub_id, response, _ = freeze_publication(conn, body.note, now)
            new_version = bump_data_version(conn)
            # 凭证标记为已使用（与冻结同一事务提交），此后再凭该凭证确认一律拒绝
            conn.execute(
                """
                UPDATE publish_checks
                SET used=1, confirmed_at=?, confirmed_publication_id=?
                WHERE token_hash=?
                """,
                (now, pub_id, row["token_hash"]),
            )

        response["data_version"] = new_version
        return response


    @app.get("/publications", tags=["发布"])
    def list_publications() -> list[dict[str, Any]]:
        with db.read() as conn:
            rows = conn.execute("SELECT * FROM publications ORDER BY id DESC").fetchall()
        return [{"publication_id": r["id"], "published_at": r["published_at"], "note": r["note"]} for r in rows]

    def _load_publication_payload(conn: sqlite3.Connection, pub: sqlite3.Row) -> dict[str, Any]:
        rows = conn.execute(
            "SELECT station_id, timeline_json FROM publication_timelines WHERE publication_id=? ORDER BY station_id",
            (pub["id"],),
        ).fetchall()
        zone_rows = conn.execute(
            "SELECT zone_id, timeline_json FROM publication_zones WHERE publication_id=? ORDER BY zone_id",
            (pub["id"],),
        ).fetchall()
        return {
            "publication_id": pub["id"],
            "published_at": pub["published_at"],
            "note": pub["note"],
            "timelines": [
                {"published": True, **json.loads(r["timeline_json"])} for r in rows
            ],
            "zone_timelines": [
                {"published": True, **json.loads(r["timeline_json"])} for r in zone_rows
            ],
        }

    @app.get("/publications/{publication_id}", tags=["发布"])
    def get_publication(publication_id: int) -> dict[str, Any]:
        with db.read() as conn:
            pub = conn.execute("SELECT * FROM publications WHERE id=?", (publication_id,)).fetchone()
            if pub is None:
                raise HTTPException(status_code=404, detail=f"发布 {publication_id} 不存在")
            return _load_publication_payload(conn, pub)

    @app.get("/public/current", tags=["公开结果"])
    def public_current() -> dict[str, Any]:
        with db.read() as conn:
            pub = conn.execute("SELECT * FROM publications ORDER BY id DESC LIMIT 1").fetchone()
            if pub is None:
                raise HTTPException(status_code=404, detail="尚未发布任何结果")
            return _load_publication_payload(conn, pub)

    @app.get("/public/stations/{station_id}/timeline", tags=["公开结果"])
    def public_station_timeline(station_id: int) -> dict[str, Any]:
        with db.read() as conn:
            get_station_or_404(conn, station_id)
            row = conn.execute(
                """
                SELECT pt.timeline_json, p.id AS pub_id, p.published_at
                FROM publication_timelines pt
                JOIN publications p ON p.id = pt.publication_id
                WHERE pt.station_id=?
                ORDER BY pt.publication_id DESC LIMIT 1
                """,
                (station_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="该监测站尚无已发布结果")
        return {
            "publication_id": row["pub_id"],
            "published_at": row["published_at"],
            "published": True,
            **json.loads(row["timeline_json"]),
        }

    # ------------------------------------------------------------ 健康检查

    @app.get("/health", tags=["系统"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
