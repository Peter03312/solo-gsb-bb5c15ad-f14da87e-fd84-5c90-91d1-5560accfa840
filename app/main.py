"""山洪水位预警裁定服务 —— FastAPI 入口。

运行：uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import json
import hashlib
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
        description="监测站/设备登记、读数补传（幂等）、审核修订、阈值版本（含不落库预演）、单站预警裁定、防汛责任区跨站联动、受控发布（核对→确认两阶段冻结）。",
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
            db.bump_adjudication_version(conn)
            row = conn.execute("SELECT * FROM stations WHERE id=?", (cur.lastrowid,)).fetchone()
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
            db.bump_adjudication_version(conn)
            row = conn.execute("SELECT * FROM devices WHERE id=?", (cur.lastrowid,)).fetchone()
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
                db.bump_adjudication_version(conn)
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
            db.bump_adjudication_version(conn)
            updated, _ = get_reading_or_404(conn, reading_id)
            station_id = conn.execute(
                "SELECT station_id FROM devices WHERE id=?", (updated["device_id"],)
            ).fetchone()["station_id"]
            timeline = compute_timeline(conn, station_id)
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
            db.bump_adjudication_version(conn)
            updated, _ = get_reading_or_404(conn, reading_id)
            station_id = conn.execute(
                "SELECT station_id FROM devices WHERE id=?", (updated["device_id"],)
            ).fetchone()["station_id"]
            timeline = compute_timeline(conn, station_id)
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
                db.bump_adjudication_version(conn)
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
            db.bump_adjudication_version(conn)
            zone = conn.execute("SELECT * FROM zones WHERE id=?", (zone_id,)).fetchone()
            members = get_zone_members(conn, zone_id)
            timeline = compute_zone_timeline(conn, zone_id)
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

    def _station_event_keys(timeline: dict[str, Any]) -> list[tuple[str, str, int]]:
        """单站事件多重集的键：(事件类型, 发生时刻, 出现次数)。

        出现次数取触发事件的连续达阈值读数个数 run_count。
        """
        return [(e["type"], e["at"], e.get("run_count", 1)) for e in timeline["events"]]

    def _zone_event_keys(timeline: dict[str, Any]) -> list[tuple[str, str, int]]:
        """责任区事件多重集的键：(事件类型, 发生时刻, 出现次数)。

        区域事件每个时刻至多一个，出现次数取事件发生时仍在预警的成员站数。
        """
        return [(e["type"], e["at"], e["warning_station_count"]) for e in timeline["events"]]

    def _diff_event_lists(
        baseline_keys: list[tuple[str, str, int]],
        pending_keys: list[tuple[str, str, int]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """按 (事件类型, 发生时刻, 出现次数) 的多重集比对，返回 (新增, 消失) 事件。

        多重集语义：同键出现 n 次则列出 n 条；结果按发生时刻、类型、出现次数排序。
        """
        base_counter: Counter = Counter(baseline_keys)
        pending_counter: Counter = Counter(pending_keys)

        def rows(counter: Counter) -> list[dict[str, Any]]:
            out = [
                {"type": ev_type, "at": at, "occurrence_count": occ}
                for (ev_type, at, occ), count in counter.items()
                for _ in range(count)
            ]
            out.sort(key=lambda e: (e["at"], e["type"], e["occurrence_count"]))
            return out

        return rows(pending_counter - base_counter), rows(base_counter - pending_counter)

    def _compute_pending_payload(conn: sqlite3.Connection) -> dict[str, Any]:
        """计算当前待发布冻结载荷（纯计算、不写库）。

        载荷结构与实际冻结写入的 JSON 内容一致，供：
        - 核对：与最新公开快照逐项比对；
        - 确认：指纹复核后原样冻结，保证“冻结内容与核对时的待发布结果一致”。
        """
        stations_payload: list[dict[str, Any]] = []
        for st in conn.execute("SELECT id FROM stations ORDER BY id").fetchall():
            stations_payload.append(compute_timeline(conn, st["id"]))
        zones_payload: list[dict[str, Any]] = []
        for z in conn.execute("SELECT id FROM zones ORDER BY id").fetchall():
            zones_payload.append(compute_zone_timeline(conn, z["id"]))
        return {"stations": stations_payload, "zones": zones_payload}

    def _freeze_payload(
        conn: sqlite3.Connection, publication_id: int, payload: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """把给定待发布载荷冻结写入 publication_timelines / publication_zones。"""
        station_summaries: list[dict[str, Any]] = []
        for timeline in payload["stations"]:
            conn.execute(
                "INSERT INTO publication_timelines(publication_id, station_id, timeline_json) VALUES (?,?,?)",
                (publication_id, timeline["station_id"], json.dumps(timeline, ensure_ascii=False)),
            )
            station_summaries.append(
                {
                    "station_id": timeline["station_id"],
                    "station_name": timeline["station_name"],
                    "status": timeline["status"],
                    "event_count": len(timeline["events"]),
                }
            )
        zone_summaries: list[dict[str, Any]] = []
        for ztl in payload["zones"]:
            conn.execute(
                "INSERT INTO publication_zones(publication_id, zone_id, timeline_json) VALUES (?,?,?)",
                (publication_id, ztl["zone_id"], json.dumps(ztl, ensure_ascii=False)),
            )
            zone_summaries.append(
                {
                    "zone_id": ztl["zone_id"],
                    "zone_name": ztl["zone_name"],
                    "status": ztl["status"],
                    "event_count": len(ztl["events"]),
                }
            )
        return station_summaries, zone_summaries

    def _payload_fingerprint(payload: dict[str, Any]) -> str:
        """待发布冻结内容指纹：规范化 JSON（键排序、紧凑分隔）后的 SHA-256。"""
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _load_baseline_snapshot(
        conn: sqlite3.Connection,
    ) -> tuple[Optional[int], dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
        """加载最新公开快照：(发布编号, {站id: 冻结时间线}, {责任区id: 冻结时间线})。

        尚无任何发布时返回 (None, {}, {})，即首次发布/新对象的空基准。
        """
        pub = conn.execute("SELECT * FROM publications ORDER BY id DESC LIMIT 1").fetchone()
        if pub is None:
            return None, {}, {}
        station_rows = conn.execute(
            "SELECT station_id, timeline_json FROM publication_timelines WHERE publication_id=?",
            (pub["id"],),
        ).fetchall()
        zone_rows = conn.execute(
            "SELECT zone_id, timeline_json FROM publication_zones WHERE publication_id=?",
            (pub["id"],),
        ).fetchall()
        return (
            pub["id"],
            {r["station_id"]: json.loads(r["timeline_json"]) for r in station_rows},
            {r["zone_id"]: json.loads(r["timeline_json"]) for r in zone_rows},
        )

    def _build_reconcile_result(
        conn: sqlite3.Connection,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """核对当前待发布裁定相对最新公开快照的变化。

        返回 (核对结果响应体, 待发布冻结载荷)；首次发布以空快照为基准，
        快照之后新登记的站 / 责任区按空基准（baseline_status=null）比较。
        """
        baseline_id, base_stations, base_zones = _load_baseline_snapshot(conn)
        pending = _compute_pending_payload(conn)
        now = iso_z(utc_now())

        station_entries: list[dict[str, Any]] = []
        zone_entries: list[dict[str, Any]] = []

        def append_entry(
            obj_id: int,
            obj_name: str,
            id_field: str,
            name_field: str,
            baseline: Optional[dict[str, Any]],
            current: Optional[dict[str, Any]],
            event_keys,
        ) -> dict[str, Any]:
            base_keys = event_keys(baseline) if baseline is not None else []
            pending_keys = event_keys(current) if current is not None else []
            added, disappeared = _diff_event_lists(base_keys, pending_keys)
            entry = {
                id_field: obj_id,
                name_field: obj_name,
                "baseline_status": baseline["status"] if baseline is not None else None,
                "pending_status": current["status"] if current is not None else None,
                "new_object": baseline is None,
                "added_events": added,
                "disappeared_events": disappeared,
            }
            (station_entries if id_field == "station_id" else zone_entries).append(entry)
            return entry

        pending_station_ids = {tl["station_id"] for tl in pending["stations"]}
        for tl in pending["stations"]:
            append_entry(
                tl["station_id"], tl["station_name"], "station_id", "station_name",
                base_stations.get(tl["station_id"]), tl, _station_event_keys,
            )
        # 防御：当前不存在但历史快照中有的站（现有接口无删除，一般不会出现）
        for sid, base in base_stations.items():
            if sid not in pending_station_ids:
                append_entry(
                    sid, base.get("station_name", str(sid)), "station_id", "station_name",
                    base, None, _station_event_keys,
                )

        pending_zone_ids = {ztl["zone_id"] for ztl in pending["zones"]}
        for ztl in pending["zones"]:
            append_entry(
                ztl["zone_id"], ztl["zone_name"], "zone_id", "zone_name",
                base_zones.get(ztl["zone_id"]), ztl, _zone_event_keys,
            )
        for zid, base in base_zones.items():
            if zid not in pending_zone_ids:
                append_entry(
                    zid, base.get("zone_name", str(zid)), "zone_id", "zone_name",
                    base, None, _zone_event_keys,
                )

        def entry_changed(entry: dict[str, Any]) -> bool:
            return (
                entry["baseline_status"] != entry["pending_status"]
                or bool(entry["added_events"])
                or bool(entry["disappeared_events"])
            )

        added_event_count = sum(
            len(e["added_events"]) for e in station_entries + zone_entries
        )
        disappeared_event_count = sum(
            len(e["disappeared_events"]) for e in station_entries + zone_entries
        )
        new_station_ids = [e["station_id"] for e in station_entries if e["new_object"]]
        new_zone_ids = [e["zone_id"] for e in zone_entries if e["new_object"]]

        result = {
            "baseline_publication_id": baseline_id,
            "checked_at": now,
            "stations": station_entries,
            "zones": zone_entries,
            "summary": {
                "station_count": len(station_entries),
                "zone_count": len(zone_entries),
                "new_station_ids": new_station_ids,
                "new_zone_ids": new_zone_ids,
                "stations_changed": sum(1 for e in station_entries if entry_changed(e)),
                "zones_changed": sum(1 for e in zone_entries if entry_changed(e)),
                "added_event_count": added_event_count,
                "disappeared_event_count": disappeared_event_count,
                "has_changes": (
                    bool(new_station_ids)
                    or bool(new_zone_ids)
                    or added_event_count > 0
                    or disappeared_event_count > 0
                    or any(e["baseline_status"] != e["pending_status"]
                           for e in station_entries + zone_entries)
                ),
            },
        }
        return result, pending

    @app.post("/publish", status_code=201, tags=["发布"])
    def publish(body: Optional[PublishRequest] = None) -> dict[str, Any]:
        note = body.note if body else None
        now = iso_z(utc_now())
        with db.write_tx() as conn:
            cur = conn.execute(
                "INSERT INTO publications(published_at, note) VALUES (?,?)", (now, note)
            )
            pub_id = cur.lastrowid
            payload = _compute_pending_payload(conn)
            station_summaries, zone_summaries = _freeze_payload(conn, pub_id, payload)
            # 新发布本身使所有未确认凭证的“其他发布先完成”条件失效
            db.bump_adjudication_version(conn)
        return {
            "publication_id": pub_id,
            "published_at": now,
            "note": note,
            "stations": station_summaries,
            "zones": zone_summaries,
        }

    @app.post("/publish/reconcile", status_code=201, tags=["发布"])
    def publish_reconcile() -> dict[str, Any]:
        """受控发布第一步：核对当前待发布裁定相对最新公开快照的变化。

        - 返回基准发布编号（首次发布为 null）、各站/责任区状态的前后值、
          按（对象、事件类型、发生时刻、出现次数）多重集比对的新增/消失事件，
          以及一次性确认凭证 confirmation_token；
        - 新登记对象以空基准比较（baseline_status=null，全部事件计入新增）；
        - 核对不产生发布记录，也不改变待发布裁定；核对结果（含待发布内容指纹、
          裁定数据版本、基准发布编号）随凭证落库，供确认时复核。
        """
        now = iso_z(utc_now())
        token = secrets.token_urlsafe(24)
        with db.write_tx() as conn:
            result, pending_payload = _build_reconcile_result(conn)
            conn.execute(
                """
                INSERT INTO release_confirmations(
                    token, status, baseline_publication_id, adjudication_version,
                    checked_at, result_json, pending_fingerprint)
                VALUES (?, 'open', ?, ?, ?, ?, ?)
                """,
                (
                    token,
                    result["baseline_publication_id"],
                    db.adjudication_version(conn),
                    now,
                    json.dumps(result, ensure_ascii=False),
                    _payload_fingerprint(pending_payload),
                ),
            )
        return {**result, "confirmation_token": token}

    @app.post("/publish/confirm", status_code=201, tags=["发布"])
    def publish_confirm(body: PublishConfirmRequest) -> dict[str, Any]:
        """受控发布第二步：凭核对凭证完成受控发布。

        校验与冻结在**同一写事务**内完成：
        - 凭证不存在 → 400（invalid_token）；凭证已成功使用 → 409（token_used）；
        - 核对后发生过影响裁定的写入（站点/设备/读数补传或修订/阈值版本/责任区登记）
          或有其他发布先完成（裁定版本推进 / 基准发布编号变化 / 待发布内容指纹变化）
          → 409（拒绝，不生成发布记录、不改动凭证）；
        - 成功时冻结内容与核对时的待发布结果一致，凭证置为 confirmed，只能使用一次。
        """
        now = iso_z(utc_now())
        with db.write_tx() as conn:
            token_row = conn.execute(
                "SELECT * FROM release_confirmations WHERE token=?", (body.confirmation_token,)
            ).fetchone()
            if token_row is None:
                raise HTTPException(
                    status_code=400,
                    detail={"reason": "invalid_token", "message": "确认凭证无效"},
                )
            if token_row["status"] != "open":
                raise HTTPException(
                    status_code=409,
                    detail={
                        "reason": "token_used",
                        "message": "确认凭证已使用，不能重复确认",
                        "publication_id": token_row["confirmed_publication_id"],
                    },
                )

            latest_pub_id = conn.execute(
                "SELECT MAX(id) AS id FROM publications"
            ).fetchone()["id"]
            pending_payload = _compute_pending_payload(conn)
            current_fingerprint = _payload_fingerprint(pending_payload)
            if (
                db.adjudication_version(conn) != token_row["adjudication_version"]
                or latest_pub_id != token_row["baseline_publication_id"]
                or current_fingerprint != token_row["pending_fingerprint"]
            ):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "reason": "stale_confirmation",
                        "message": "核对后待发布裁定或基准发布已变化，凭证已失效，请重新核对",
                        "baseline_publication_id": token_row["baseline_publication_id"],
                        "checked_at": token_row["checked_at"],
                    },
                )

            cur = conn.execute(
                "INSERT INTO publications(published_at, note) VALUES (?,?)", (now, body.note)
            )
            pub_id = cur.lastrowid
            station_summaries, zone_summaries = _freeze_payload(conn, pub_id, pending_payload)
            conn.execute(
                """
                UPDATE release_confirmations
                SET status='confirmed', note=?, confirmed_publication_id=?, confirmed_at=?
                WHERE token=?
                """,
                (body.note, pub_id, now, body.confirmation_token),
            )
            # 发布完成，推进版本：使其他尚未确认的凭证随之失效
            db.bump_adjudication_version(conn)
            checked = json.loads(token_row["result_json"])

        return {
            "publication_id": pub_id,
            "published_at": now,
            "note": body.note,
            "stations": station_summaries,
            "zones": zone_summaries,
            "confirmation_token": body.confirmation_token,
            "baseline_publication_id": checked["baseline_publication_id"],
        }

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
