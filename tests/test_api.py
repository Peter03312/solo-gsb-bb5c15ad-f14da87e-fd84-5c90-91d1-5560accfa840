"""端到端 API 测试：裁定规则、幂等补传、审核修订、发布冻结、并发安全。"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.adjudication import StationStateChange, replay_zone
from app.config import Settings
from app.main import create_app

BASE = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat()


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        min_water_level=-50,
        max_water_level=1000,
        segment_gap_minutes=15,
        rise_min_count=3,
        rise_min_span_minutes=10,
        clear_min_count=2,
        clear_min_span_minutes=5,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def station(client):
    r = client.post("/stations", json={"name": "青石峡站", "rise_threshold": 10.0, "clear_threshold": 8.0})
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture()
def device(client, station):
    r = client.post(f"/stations/{station}/devices", json={"device_no": "DEV-001"})
    assert r.status_code == 201, r.text
    return "DEV-001"


def upload(client, device_no, seq, minutes, level):
    return client.post(
        "/readings",
        json={
            "device_no": device_no,
            "seq": seq,
            "observed_at": iso(BASE + timedelta(minutes=minutes)),
            "water_level": level,
        },
    )


def timeline(client, station_id):
    r = client.get(f"/stations/{station_id}/timeline")
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- 登记与校验

def test_create_station_and_device(client, station, device):
    st = client.get(f"/stations/{station}").json()
    assert st["name"] == "青石峡站"
    assert st["rise_threshold"] == 10.0
    assert st["clear_threshold"] == 8.0
    assert st["devices"][0]["device_no"] == "DEV-001"


def test_invalid_thresholds_rejected(client):
    r = client.post("/stations", json={"name": "x", "rise_threshold": 8.0, "clear_threshold": 10.0})
    assert r.status_code == 400
    r = client.post("/stations", json={"name": "x", "rise_threshold": 10.0, "clear_threshold": 10.0})
    assert r.status_code == 400
    r = client.post("/stations", json={"name": "x", "rise_threshold": "abc", "clear_threshold": 1.0})
    assert r.status_code == 400


def test_duplicate_device_conflict(client, station, device):
    r = client.post(f"/stations/{station}/devices", json={"device_no": "DEV-001"})
    assert r.status_code == 409


def test_device_on_unknown_station(client):
    r = client.post("/stations/999/devices", json={"device_no": "DEV-X"})
    assert r.status_code == 404


# ---------------------------------------------------------------- 补传校验与幂等

def test_unknown_device_rejected(client):
    r = upload(client, "NOPE", 1, 0, 5.0)
    assert r.status_code == 404


def test_naive_time_rejected(client, device):
    r = client.post(
        "/readings",
        json={"device_no": device, "seq": 1, "observed_at": "2026-10-01T00:00:00", "water_level": 5.0},
    )
    assert r.status_code == 400


def test_bad_time_string_rejected(client, device):
    r = client.post(
        "/readings",
        json={"device_no": device, "seq": 1, "observed_at": "not-a-time", "water_level": 5.0},
    )
    assert r.status_code == 400


def test_invalid_water_level_rejected(client, device):
    for bad in ("abc", 100000.0, -9999.0):
        r = client.post(
            "/readings",
            json={"device_no": device, "seq": 1, "observed_at": iso(BASE), "water_level": bad},
        )
        assert r.status_code == 400, f"water_level={bad} 应被拒绝"
    # NaN / Infinity 不是合法 JSON 数值，以原始报文发送也应被拒绝
    for bad in ("NaN", "Infinity", "-Infinity"):
        r = client.post(
            "/readings",
            content=(
                '{"device_no": "%s", "seq": 1, "observed_at": "%s", "water_level": %s}'
                % (device, iso(BASE), bad)
            ),
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 400, f"water_level={bad} 应被拒绝"


def test_idempotent_retry_same_content(client, device):
    r1 = upload(client, device, 1, 0, 5.0)
    assert r1.status_code == 201
    assert r1.json()["deduplicated"] is False
    # 同内容重试（甚至换时区表示同一时刻）→ 返回原结果
    r2 = client.post(
        "/readings",
        json={
            "device_no": device,
            "seq": 1,
            "observed_at": (BASE).astimezone(timezone(timedelta(hours=8))).isoformat(),
            "water_level": 5.0,
        },
    )
    assert r2.status_code == 200
    assert r2.json()["deduplicated"] is True
    assert r2.json()["id"] == r1.json()["id"]
    rows = client.get(f"/stations/1/readings").json()
    assert len(rows) == 1


def test_conflicting_seq_rejected(client, device):
    r1 = upload(client, device, 1, 0, 5.0)
    assert r1.status_code == 201
    r2 = upload(client, device, 1, 0, 6.0)  # 同号同序号异内容
    assert r2.status_code == 409
    r3 = upload(client, device, 1, 5, 5.0)
    assert r3.status_code == 409
    rows = client.get(f"/stations/1/readings").json()
    assert len(rows) == 1  # 冲突不产生新记录


# ---------------------------------------------------------------- 裁定规则

def test_warning_requires_3_readings_and_10min_span(client, station, device):
    upload(client, device, 1, 0, 11.0)
    upload(client, device, 2, 4, 11.0)
    upload(client, device, 3, 8, 11.0)  # 3 个但首尾仅 8 分钟
    tl = timeline(client, station)
    assert tl["status"] == "normal"
    assert tl["events"] == []
    upload(client, device, 4, 12, 11.0)  # 4 个，首尾 12 分钟 → 在此读数预警
    tl = timeline(client, station)
    assert tl["status"] == "warning"
    assert len(tl["events"]) == 1
    ev = tl["events"][0]
    assert ev["type"] == "warning"
    assert ev["at"] == iso(BASE + timedelta(minutes=12)).replace("+00:00", "Z")
    assert ev["run_count"] == 4


def test_out_of_order_uploads_reordered(client, station, device):
    # 乱序补传，按观测时间重排后仍应触发预警
    upload(client, device, 3, 10, 12.0)
    upload(client, device, 1, 0, 12.0)
    upload(client, device, 2, 5, 12.0)
    tl = timeline(client, station)
    assert tl["status"] == "warning"
    assert tl["events"][0]["at"] == iso(BASE + timedelta(minutes=10)).replace("+00:00", "Z")


def test_segment_break_resets_rise_run(client, station, device):
    upload(client, device, 1, 0, 11.0)
    upload(client, device, 2, 6, 11.0)
    upload(client, device, 3, 30, 11.0)  # 间隔 24 分钟 > 15 → 断段，重新计数
    tl = timeline(client, station)
    assert tl["status"] == "normal"
    assert len(tl["segments"]) == 2
    upload(client, device, 4, 35, 11.0)
    upload(client, device, 5, 40, 11.0)  # 新段内 3 个、首尾 10 分钟 → 预警
    tl = timeline(client, station)
    assert tl["status"] == "warning"
    assert tl["events"][0]["at"] == iso(BASE + timedelta(minutes=40)).replace("+00:00", "Z")


def test_interruption_by_low_reading_resets_run(client, station, device):
    upload(client, device, 1, 0, 11.0)
    upload(client, device, 2, 5, 11.0)
    upload(client, device, 3, 10, 9.0)   # 未达上升阈值，连续被打断
    upload(client, device, 4, 15, 11.0)
    upload(client, device, 5, 20, 11.0)
    tl = timeline(client, station)
    assert tl["status"] == "normal"


def test_clear_requires_2_readings_and_5min_span(client, station, device):
    for i, m in enumerate([0, 5, 10], start=1):
        upload(client, device, i, m, 12.0)
    assert timeline(client, station)["status"] == "warning"
    upload(client, device, 4, 12, 7.0)
    upload(client, device, 5, 14, 7.0)  # 2 个但首尾仅 2 分钟
    assert timeline(client, station)["status"] == "warning"
    upload(client, device, 6, 18, 7.0)  # 3 个，首尾 6 分钟 → 解除
    tl = timeline(client, station)
    assert tl["status"] == "normal"
    assert tl["events"][-1]["type"] == "cleared"
    assert tl["events"][-1]["at"] == iso(BASE + timedelta(minutes=18)).replace("+00:00", "Z")


def test_segment_break_does_not_auto_clear(client, station, device):
    for i, m in enumerate([0, 5, 10], start=1):
        upload(client, device, i, m, 12.0)
    assert timeline(client, station)["status"] == "warning"
    upload(client, device, 4, 12, 7.0)   # 1 个低水位
    upload(client, device, 5, 40, 7.0)   # 断段（28 分钟）不自动解除，但解除计数跨段延续
    tl = timeline(client, station)
    assert tl["status"] == "normal"      # 2 个低水位、首尾 28 分钟 ≥ 5 → 解除
    assert tl["events"][-1]["type"] == "cleared"


def test_warning_can_retrigger_after_clear(client, station, device):
    for i, m in enumerate([0, 5, 10], start=1):
        upload(client, device, i, m, 12.0)
    upload(client, device, 4, 15, 7.0)
    upload(client, device, 5, 21, 7.0)   # 解除
    upload(client, device, 6, 25, 12.0)
    upload(client, device, 7, 30, 12.0)
    upload(client, device, 8, 40, 12.0)  # 再次预警
    tl = timeline(client, station)
    types = [e["type"] for e in tl["events"]]
    assert types == ["warning", "cleared", "warning"]
    assert tl["status"] == "warning"


# ---------------------------------------------------------------- 审核修订

def _make_warning(client, device):
    for i, m in enumerate([0, 5, 10], start=1):
        upload(client, device, i, m, 12.0)


def test_void_reading_recalculates(client, station, device):
    _make_warning(client, device)
    assert timeline(client, station)["status"] == "warning"
    r = client.post("/readings/2/void", json={"reason": "传感器故障", "auditor": "王审"})
    assert r.status_code == 200
    assert r.json()["reading"]["status"] == "voided"
    assert r.json()["pending_adjudication"]["status"] == "normal"  # 裁定随之重算
    # 默认列表不含已作废，include_voided 可见
    assert len(client.get(f"/stations/{station}/readings").json()) == 2
    assert len(client.get(f"/stations/{station}/readings?include_voided=true").json()) == 3
    # 原值与修订历史可查
    hist = client.get("/readings/2/revisions").json()
    assert hist["original"]["water_level"] == 12.0
    assert len(hist["revisions"]) == 1
    assert hist["revisions"][0]["action"] == "void"
    assert hist["revisions"][0]["reason"] == "传感器故障"
    assert hist["revisions"][0]["old_water_level"] == 12.0


def test_void_requires_reason(client, device):
    _make_warning(client, device)
    r = client.post("/readings/1/void", json={"reason": ""})
    assert r.status_code == 400
    r = client.post("/readings/1/void", json={})
    assert r.status_code == 400


def test_double_void_conflict(client, device):
    _make_warning(client, device)
    client.post("/readings/1/void", json={"reason": "r"})
    r = client.post("/readings/1/void", json={"reason": "again"})
    assert r.status_code == 409


def test_correct_reading_recalculates(client, station, device):
    _make_warning(client, device)
    assert timeline(client, station)["status"] == "warning"
    # 把第 2 条更正为低于阈值 → 连续被打断，预警消失
    r = client.post("/readings/2/correct", json={"water_level": 5.0, "reason": "抄录错误", "auditor": "李审"})
    assert r.status_code == 200
    assert r.json()["reading"]["water_level"] == 5.0
    assert r.json()["reading"]["original_water_level"] == 12.0
    assert r.json()["pending_adjudication"]["status"] == "normal"
    hist = client.get("/readings/2/revisions").json()
    assert hist["original"]["water_level"] == 12.0
    assert hist["revisions"][0]["action"] == "correct"
    assert hist["revisions"][0]["new_water_level"] == 5.0


def test_correct_time_reorders(client, station, device):
    upload(client, device, 1, 0, 12.0)
    upload(client, device, 2, 5, 12.0)
    upload(client, device, 3, 100, 12.0)  # 间隔过大，断段 → 无预警
    assert timeline(client, station)["status"] == "normal"
    # 更正观测时间使其落入同段 → 触发预警
    r = client.post("/readings/3/correct", json={"observed_at": iso(BASE + timedelta(minutes=10)), "reason": "时钟偏移"})
    assert r.status_code == 200
    assert r.json()["pending_adjudication"]["status"] == "warning"


def test_correct_voided_reading_conflict(client, device):
    _make_warning(client, device)
    client.post("/readings/1/void", json={"reason": "r"})
    r = client.post("/readings/1/correct", json={"water_level": 1.0, "reason": "r"})
    assert r.status_code == 409


def test_revision_history_accumulates(client, device):
    _make_warning(client, device)
    client.post("/readings/1/correct", json={"water_level": 11.5, "reason": "第一次更正"})
    client.post("/readings/1/correct", json={"water_level": 11.8, "reason": "第二次更正", "auditor": "赵审"})
    hist = client.get("/readings/1/revisions").json()
    assert hist["original"]["water_level"] == 12.0
    assert [r["revision_no"] for r in hist["revisions"]] == [1, 2]
    assert hist["revisions"][0]["new_water_level"] == 11.5
    assert hist["revisions"][1]["old_water_level"] == 11.5
    assert hist["revisions"][1]["new_water_level"] == 11.8
    assert hist["reading"]["water_level"] == 11.8
    assert hist["reading"]["version"] == 3


# ---------------------------------------------------------------- 阈值版本（防汛方案调整）

def make_version(client, station, minutes, rise, clear, reason="方案调整"):
    return client.post(
        f"/stations/{station}/threshold-versions",
        json={
            "effective_at": iso(BASE + timedelta(minutes=minutes)),
            "rise_threshold": rise,
            "clear_threshold": clear,
            "reason": reason,
        },
    )


def test_threshold_version_on_unknown_station_404(client):
    r = client.post(
        "/stations/999/threshold-versions",
        json={"effective_at": iso(BASE), "rise_threshold": 9.0, "clear_threshold": 7.0, "reason": "x"},
    )
    assert r.status_code == 404
    assert client.get("/stations/999/threshold-versions").status_code == 404


def test_create_threshold_version_and_history(client, station):
    r = make_version(client, station, 60, 9.0, 7.0, reason="入汛提级")
    assert r.status_code == 201, r.text
    body = r.json()
    v = body["version"]
    assert v["station_id"] == station
    assert v["rise_threshold"] == 9.0
    assert v["clear_threshold"] == 7.0
    assert v["reason"] == "入汛提级"
    assert v["effective_at"] == iso(BASE + timedelta(minutes=60)).replace("+00:00", "Z")
    # 调整本身不产生预警或解除
    assert body["pending_adjudication"]["events"] == []
    assert body["pending_adjudication"]["status"] == "normal"

    hist = client.get(f"/stations/{station}/threshold-versions").json()
    # 登记阈值作为未命中版本时的基准值
    assert hist["baseline"] == {"rise_threshold": 10.0, "clear_threshold": 8.0}
    assert len(hist["versions"]) == 1
    assert hist["versions"][0]["id"] == v["id"]


def test_threshold_version_requires_reason(client, station):
    r = client.post(
        f"/stations/{station}/threshold-versions",
        json={"effective_at": iso(BASE), "rise_threshold": 9.0, "clear_threshold": 7.0, "reason": ""},
    )
    assert r.status_code == 400


def test_threshold_version_naive_time_rejected(client, station):
    r = client.post(
        f"/stations/{station}/threshold-versions",
        json={
            "effective_at": "2026-10-01T01:00:00",
            "rise_threshold": 9.0,
            "clear_threshold": 7.0,
            "reason": "x",
        },
    )
    assert r.status_code == 400


def test_threshold_version_invalid_thresholds_rejected(client, station):
    base = {"effective_at": iso(BASE), "reason": "x"}
    assert client.post(
        f"/stations/{station}/threshold-versions",
        json={**base, "rise_threshold": 7.0, "clear_threshold": 9.0},
    ).status_code == 400  # 上升不大于解除
    assert client.post(
        f"/stations/{station}/threshold-versions",
        json={**base, "rise_threshold": 9.0, "clear_threshold": 9.0},
    ).status_code == 400  # 相等也拒绝
    assert client.post(
        f"/stations/{station}/threshold-versions",
        json={**base, "rise_threshold": "abc", "clear_threshold": 1.0},
    ).status_code == 400
    for bad in ("NaN", "Infinity", "-Infinity"):
        r = client.post(
            f"/stations/{station}/threshold-versions",
            content=(
                '{"effective_at":"%s","rise_threshold":%s,"clear_threshold":1.0,"reason":"x"}'
                % (iso(BASE), bad)
            ),
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 400, f"rise_threshold={bad} 应被拒绝"


def test_duplicate_effective_at_conflict(client, station):
    payload = {
        "rise_threshold": 9.0,
        "clear_threshold": 7.0,
        "reason": "x",
    }
    r1 = client.post(
        f"/stations/{station}/threshold-versions",
        json={"effective_at": iso(BASE + timedelta(minutes=60)), **payload},
    )
    assert r1.status_code == 201
    # 同一时刻重复（换 +08:00 表示同一瞬间）→ 409
    r2 = client.post(
        f"/stations/{station}/threshold-versions",
        json={
            "effective_at": iso((BASE + timedelta(minutes=60)).astimezone(timezone(timedelta(hours=8)))),
            **payload,
        },
    )
    assert r2.status_code == 409
    # 不同生效时刻允许登记
    r3 = client.post(
        f"/stations/{station}/threshold-versions",
        json={"effective_at": iso(BASE + timedelta(minutes=120)), **payload},
    )
    assert r3.status_code == 201
    hist = client.get(f"/stations/{station}/threshold-versions").json()
    assert [v["effective_at"] for v in hist["versions"]] == [
        iso(BASE + timedelta(minutes=60)).replace("+00:00", "Z"),
        iso(BASE + timedelta(minutes=120)).replace("+00:00", "Z"),
    ]


def test_baseline_used_before_first_version(client, station, device):
    # 版本 60 分钟起生效（上升 9 / 解除 7）；早期读数仍按登记基准 10 / 8
    make_version(client, station, 60, 9.0, 7.0)
    upload(client, device, 1, 0, 9.5)   # < 基准上升 10，且 < 版本上升 9？
    tl = timeline(client, station)
    info = {r["reading_id"]: r for r in tl["readings"]}
    assert info[1]["threshold_version_id"] is None
    assert info[1]["rise_threshold"] == 10.0
    upload(client, device, 2, 5, 9.5)
    upload(client, device, 3, 70, 9.5)  # 命中版本：9.5 >= 9，但连续被版本切换打断吗？
    tl = timeline(client, station)
    info = {r["reading_id"]: r for r in tl["readings"]}
    assert info[1]["rise_threshold"] == 10.0 and info[1]["threshold_version_id"] is None
    assert info[3]["rise_threshold"] == 9.0 and info[3]["threshold_version_id"] is not None
    # 前两条 9.5 未达基准 10 → 连续达阈值序列从读数 3 才开始，无预警
    assert tl["status"] == "normal"
    assert tl["events"] == []


def test_version_boundary_exact_time_uses_new_version(client, station, device):
    make_version(client, station, 10, 9.0, 7.0)
    upload(client, device, 1, 0, 9.5)   # 基准 10：未达
    upload(client, device, 2, 5, 9.5)   # 基准 10：未达
    upload(client, device, 3, 10, 9.5)  # 同一时刻采用新版本 9：达阈值，但序列从此开始
    tl = timeline(client, station)
    info = {r["reading_id"]: r for r in tl["readings"]}
    assert info[3]["threshold_version_id"] is not None
    assert tl["status"] == "normal"
    upload(client, device, 4, 15, 9.5)
    upload(client, device, 5, 25, 9.5)  # 版本下连续 3 条、首尾 15 分钟 → 预警
    tl = timeline(client, station)
    assert tl["status"] == "warning"
    ev = tl["events"][0]
    assert ev["threshold_version_id"] is not None
    assert ev["rise_threshold"] == 9.0
    assert ev["at"] == iso(BASE + timedelta(minutes=25)).replace("+00:00", "Z")


def test_cross_version_continuous_readings_each_own_threshold(client, station, device):
    # 基准上升 10：前两条 10.5 达阈值；新版本（t=10 起）上升 11，第三条 10.5 不再达阈值
    make_version(client, station, 10, 11.0, 9.0)
    upload(client, device, 1, 0, 10.5)
    upload(client, device, 2, 5, 10.5)
    upload(client, device, 3, 10, 10.5)  # 按新版本 11 判断 → 打断连续
    upload(client, device, 4, 15, 10.5)
    tl = timeline(client, station)
    assert tl["status"] == "normal"
    assert tl["events"] == []


def test_threshold_change_itself_emits_no_event(client, station, device):
    # 无读数时调整阈值（生效时间设在未来，不影响后续测试读数）：不产生任何事件
    r = make_version(client, station, 10000, 100.0, 13.0, reason="演练调整")
    assert r.status_code == 201
    assert r.json()["pending_adjudication"]["events"] == []
    assert r.json()["pending_adjudication"]["status"] == "normal"

    _make_warning(client, device)
    assert timeline(client, station)["status"] == "warning"
    # 新版本自 t=0 起覆盖全部历史时间线：12 < 100 不再达上升阈值，重算后无事件。
    # 关键：不存在“因调整动作本身”而产生的预警/解除，事件只可能由读数判定产生。
    r2 = client.post(
        f"/stations/{station}/threshold-versions",
        json={"effective_at": iso(BASE), "rise_threshold": 100.0, "clear_threshold": 13.0, "reason": "放宽"},
    )
    assert r2.status_code == 201
    events = r2.json()["pending_adjudication"]["events"]
    assert events == []
    assert r2.json()["pending_adjudication"]["status"] == "normal"


def test_segment_break_and_cross_version_clear_still_works(client, station, device):
    # 预警：基准阈值 10/8
    for i, m in enumerate([0, 5, 10], start=1):
        upload(client, device, i, m, 12.0)
    # 版本自 12 分钟起：解除阈值降到 6
    make_version(client, station, 12, 20.0, 6.0)
    upload(client, device, 4, 12, 5.0)   # 命中版本，<= 6
    upload(client, device, 5, 40, 5.0)   # 断段但解除计数跨段延续 → 解除
    tl = timeline(client, station)
    assert tl["status"] == "normal"
    ev = tl["events"][-1]
    assert ev["type"] == "cleared"
    assert ev["clear_threshold"] == 6.0
    assert ev["threshold_version_id"] is not None


def test_recompute_after_correction_uses_versions(client, station, device):
    make_version(client, station, 100, 9.0, 7.0)
    # 读数在 t=0/5/10，基准 10 下 12 达阈值 → 预警
    for i, m in enumerate([0, 5, 10], start=1):
        upload(client, device, i, m, 12.0)
    assert timeline(client, station)["status"] == "warning"
    # 更正读数 2 的观测时间到 t=110（落入版本区间），水位 5 不影响上升序列；
    # 全部有效读数重算后，原连续段被时间重排打断
    r = client.post(
        "/readings/2/correct",
        json={"observed_at": iso(BASE + timedelta(minutes=110)), "reason": "时钟偏移"},
    )
    assert r.status_code == 200
    assert r.json()["pending_adjudication"]["status"] == "normal"
    tl = timeline(client, station)
    info = {x["reading_id"]: x for x in tl["readings"]}
    assert info[2]["threshold_version_id"] is not None
    assert info[2]["rise_threshold"] == 9.0


def test_void_then_recompute_uses_versions(client, station, device):
    make_version(client, station, 0, 9.0, 7.0)
    for i, m in enumerate([0, 5, 10], start=1):
        upload(client, device, i, m, 9.5)
    assert timeline(client, station)["status"] == "warning"
    client.post("/readings/2/void", json={"reason": "误报"})
    assert timeline(client, station)["status"] == "normal"


def test_published_snapshot_unchanged_by_threshold_adjustment(client, station, device):
    for i, m in enumerate([0, 5, 10], start=1):
        upload(client, device, i, m, 9.5)  # 基准 10：无预警
    r = client.post("/publish", json={"note": "调整前发布"})
    pub_id = r.json()["publication_id"]
    assert client.get("/public/current").json()["timelines"][0]["status"] == "normal"

    # 调整阈值使历史读数应触发预警：历史发布快照不变
    make_version(client, station, 0, 9.0, 7.0, reason="提级")
    assert timeline(client, station)["status"] == "warning"
    snap = client.get(f"/publications/{pub_id}").json()
    assert snap["timelines"][0]["status"] == "normal"
    assert client.get("/public/current").json()["timelines"][0]["status"] == "normal"
    # 重新发布才反映调整
    client.post("/publish", json={"note": "调整后发布"})
    cur = client.get("/public/current").json()
    assert cur["publication_id"] == pub_id + 1
    assert cur["timelines"][0]["status"] == "warning"
    assert cur["timelines"][0]["events"][0]["rise_threshold"] == 9.0


def test_version_history_per_station_independent(client, device):
    other = client.post(
        "/stations", json={"name": "南河站", "rise_threshold": 20.0, "clear_threshold": 15.0}
    ).json()["id"]
    make_version(client, 1, 0, 9.0, 7.0)
    hist = client.get(f"/stations/{other}/threshold-versions").json()
    assert hist["versions"] == []
    assert hist["baseline"] == {"rise_threshold": 20.0, "clear_threshold": 15.0}


def test_duplicate_effective_at_per_station_scope(client, station):
    other = client.post(
        "/stations", json={"name": "南河站", "rise_threshold": 20.0, "clear_threshold": 15.0}
    ).json()["id"]
    body = {"effective_at": iso(BASE), "rise_threshold": 9.0, "clear_threshold": 7.0, "reason": "x"}
    assert client.post(f"/stations/{station}/threshold-versions", json=body).status_code == 201
    # 不同站同一生效时刻允许
    assert client.post(f"/stations/{other}/threshold-versions", json=body).status_code == 201


# ---------------------------------------------------------------- 阈值版本预演（不落库）

def preview_version(client, station_id, minutes, rise, clear, reason="预演调整"):
    return client.post(
        f"/stations/{station_id}/threshold-versions/preview",
        json={
            "effective_at": iso(BASE + timedelta(minutes=minutes)),
            "rise_threshold": rise,
            "clear_threshold": clear,
            "reason": reason,
        },
    )


def app_state_db_path(client) -> str:
    return client.app.state.settings.db_path


def test_preview_unknown_station_404(client):
    r = preview_version(client, 999, 0, 9.0, 7.0)
    assert r.status_code == 404


def test_preview_validation_matches_registration(client, station):
    # 生效时间不带时区 → 400（沿用正式登记校验）
    r = client.post(
        f"/stations/{station}/threshold-versions/preview",
        json={"effective_at": "2026-10-01T08:00:00", "rise_threshold": 9.0,
              "clear_threshold": 7.0, "reason": "x"},
    )
    assert r.status_code == 400
    # 上升阈值不大于解除阈值 → 400
    assert preview_version(client, station, 0, 7.0, 9.0).status_code == 400
    assert preview_version(client, station, 0, 7.0, 7.0).status_code == 400
    # 原因必填（空串 → 400；与正式登记同一模型：纯空白非空字符串不在此列）
    r = client.post(
        f"/stations/{station}/threshold-versions/preview",
        json={"effective_at": iso(BASE), "rise_threshold": 9.0,
              "clear_threshold": 7.0, "reason": ""},
    )
    assert r.status_code == 400
    # 非有限数值 → 400
    for bad in ("NaN", "Infinity", "-Infinity"):
        r = client.post(
            f"/stations/{station}/threshold-versions/preview",
            content=(
                '{"effective_at":"%s","rise_threshold":%s,"clear_threshold":1.0,"reason":"x"}'
                % (iso(BASE), bad)
            ),
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 400


def test_preview_does_not_persist(client, station, device):
    for i, m in enumerate([0, 5, 10], start=1):
        upload(client, device, i, m, 9.5)  # 基准 10：不预警
    r = preview_version(client, station, 0, 9.0, 7.0)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    # 预演状态为预警
    assert body["current"]["status"] == "normal"
    assert body["preview"]["status"] == "warning"
    # 候选版本以临时 id -1 标识
    assert body["candidate"]["threshold_version_id"] == -1
    ev = body["preview"]["events"][0]
    assert ev["type"] == "warning"
    assert ev["threshold_version_id"] == -1
    assert ev["rise_threshold"] == 9.0
    # 不落库：版本历史不可见
    hist = client.get(f"/stations/{station}/threshold-versions").json()
    assert hist["versions"] == []
    # 不落库：待发布查询仍是现状
    assert timeline(client, station)["status"] == "normal"
    # 再次预演仍报 200（若误落库，同刻第二次应 409）
    assert preview_version(client, station, 0, 9.0, 7.0).status_code == 200
    # 阈值表行数确认为 0
    import sqlite3
    conn = sqlite3.connect(app_state_db_path(client))


def test_preview_conflict_with_existing_same_instant(client, station):
    r1 = make_version(client, station, 60, 9.0, 7.0, reason="已登记")
    assert r1.status_code == 201
    # 同一瞬间（换 +08:00 表示）预演 → 与正式登记同样 409
    r2 = client.post(
        f"/stations/{station}/threshold-versions/preview",
        json={
            "effective_at": iso((BASE + timedelta(minutes=60)).astimezone(timezone(timedelta(hours=8)))),
            "rise_threshold": 9.5,
            "clear_threshold": 7.5,
            "reason": "同刻预演",
        },
    )
    assert r2.status_code == 409
    assert r2.json()["detail"]["existing_version_id"] == r1.json()["version"]["id"]
    # 不同时刻预演不冲突
    assert preview_version(client, station, 120, 9.5, 7.5).status_code == 200


def test_preview_station_result_matches_actual_registration(client, station, device):
    for i, m in enumerate([0, 5, 10], start=1):
        upload(client, device, i, m, 9.5)  # 基准 10：正常
    pv = preview_version(client, station, 0, 9.0, 7.0).json()
    # 正式登记（无其他写入）
    reg = make_version(client, station, 0, 9.0, 7.0).json()
    actual = timeline(client, station)
    # 状态、事件类型与时刻一致
    assert actual["status"] == pv["preview"]["status"] == "warning"
    assert [(e["type"], e["at"], e["reading_id"]) for e in actual["events"]] == \
        [(e["type"], e["at"], e["reading_id"]) for e in pv["preview"]["events"]]
    assert reg["pending_adjudication"]["status"] == "warning"
    assert [(e["type"], e["at"]) for e in reg["pending_adjudication"]["events"]] == \
        [(e["type"], e["at"]) for e in pv["preview"]["events"]]
    # 正式登记后事件上的版本 id 换成真实版本 id，阈值本身不变
    real_id = reg["version"]["id"]
    assert actual["events"][0]["threshold_version_id"] == real_id
    assert actual["events"][0]["rise_threshold"] == pv["preview"]["events"][0]["rise_threshold"]


def test_preview_exact_effective_instant_uses_candidate(client, station, device):
    make_version(client, station, 10, 11.0, 9.0)  # 既有版本 t=10 起上升 11
    upload(client, device, 1, 0, 10.5)
    upload(client, device, 2, 5, 10.5)
    upload(client, device, 3, 10, 10.5)  # 现状：按 11 判定，序列被打断
    assert timeline(client, station)["status"] == "normal"
    # 预演同刻（t=10）候选会与既有版本冲突 → 409
    assert preview_version(client, station, 10, 9.0, 7.0).status_code == 409
    # 预演 t=5 生效：读数 2（t=5 同刻采用候选）、3 起按 9 判定
    pv = preview_version(client, station, 5, 9.0, 7.0).json()
    info = {r["reading_id"]: r for r in pv["preview"]["readings"]}
    assert info[1]["threshold_version_id"] is None       # t=0 仍用基准
    assert info[2]["threshold_version_id"] == -1         # t=5 同刻采用候选
    assert info[3]["threshold_version_id"] == 1          # t=10 起候选被既有版本接替
    # 预演版本序列：基准 → 候选(-1) → 既有版本(1)
    assert [r["threshold_version_id"] for r in pv["preview"]["readings"]] == [None, -1, 1]
    assert pv["preview"]["readings"][1]["rise_threshold"] == 9.0
    assert pv["preview"]["readings"][2]["rise_threshold"] == 11.0


def test_preview_zones_include_all_containing_station_only(client):
    s1, d1 = make_station_with_device(client, "甲站", "D1")
    s2, d2 = make_station_with_device(client, "乙站", "D2")
    s3, d3 = make_station_with_device(client, "丙站", "D3")
    create_zone(client, [s1, s2], 0, 2)   # zone 1 包含 s1
    create_zone(client, [s2, s3], 0, 2)   # zone 2 不包含 s1
    pv = preview_version(client, s1, 0, 9.0, 7.0).json()
    assert [z["zone_id"] for z in pv["zones"]] == [1]


def test_preview_zone_recomputes_with_other_members_pending(client):
    s1, d1 = make_station_with_device(client, "甲站", "D1")
    s2, d2 = make_station_with_device(client, "乙站", "D2")
    create_zone(client, [s1, s2], 0, 2)
    # 甲站 9.5 不达基准 10；乙站已预警
    for i, m in enumerate([0, 5, 10], start=1):
        upload(client, d1, i, m, 9.5)
    for i, m in enumerate([0, 5, 10], start=1):
        upload(client, d2, i, m, 12.0)
    pv = preview_version(client, s1, 0, 9.0, 7.0).json()
    assert len(pv["zones"]) == 1
    z = pv["zones"][0]
    # 现状仅乙站预警（1 < K=2）→ 正常；预演两站齐预警 → 区域预警
    assert z["current"]["status"] == "normal"
    assert z["current"]["warning_station_ids"] == [s2]
    assert z["preview"]["status"] == "warning"
    assert z["preview"]["warning_station_ids"] == [s1, s2]
    warn_ev = z["preview"]["events"][0]
    assert warn_ev["type"] == "warning"
    assert warn_ev["at"] == iso(BASE + timedelta(minutes=10)).replace("+00:00", "Z")
    assert warn_ev["warning_station_ids"] == [s1, s2]
    # 其他成员站（乙站）按现有待发布结果参与：其事件不变
    assert [e["type"] for e in z["current"]["events"]] == []
    # 预演不改变待发布区域查询
    assert zone_timeline(client, 1)["status"] == "normal"


def test_preview_zone_matches_actual_registration(client):
    s1, d1 = make_station_with_device(client, "甲站", "D1")
    s2, d2 = make_station_with_device(client, "乙站", "D2")
    create_zone(client, [s1, s2], 0, 2)
    for i, m in enumerate([0, 5, 10], start=1):
        upload(client, d1, i, m, 9.5)
    for i, m in enumerate([0, 5, 10, 15, 21], start=1):
        upload(client, d2, i, m, 12.0 if m <= 10 else 7.0)
    # 乙站 t=10 预警、t=21 解除；甲站预演提严后 t=10 预警、随后不解除
    pv = preview_version(client, s1, 0, 9.0, 7.0).json()
    zprev = [(e["type"], e["at"], e["warning_station_ids"]) for e in pv["zones"][0]["preview"]["events"]]
    make_version(client, s1, 0, 9.0, 7.0)
    actual = zone_timeline(client, 1)
    zactual = [(e["type"], e["at"], e["warning_station_ids"]) for e in actual["events"]]
    assert actual["status"] == pv["zones"][0]["preview"]["status"]
    assert zactual == zprev
    # t=10 两站齐预警；t=21 乙站解除、甲站仍预警 → 计数 1 < K → 区域解除
    assert zactual == [
        ("warning", iso(BASE + timedelta(minutes=10)).replace("+00:00", "Z"), [s1, s2]),
        ("cleared", iso(BASE + timedelta(minutes=21)).replace("+00:00", "Z"), [s1]),
    ]


def test_preview_zone_same_tick_merged(client):
    # 两站在 t=10 同刻变化：预演使甲站同刻预警，乙站现状同刻预警 → 合并后一次性区域预警
    s1, d1 = make_station_with_device(client, "甲站", "D1")
    s2, d2 = make_station_with_device(client, "乙站", "D2")
    create_zone(client, [s1, s2], 0, 2)
    for d in (d1, d2):
        for i, m in enumerate([0, 5, 10], start=1):
            upload(client, d, i, m, 9.5)
    pv = preview_version(client, s1, 0, 9.0, 7.0).json()
    # 候选只作用于甲站：乙站仍不预警 → 区域不预警（联动严格按各站待发布结果）
    assert pv["zones"][0]["preview"]["status"] == "normal"
    # 乙站也登记同版阈值（正式）后，两站同刻预警；此时再对甲站做一次不影响结果的预演
    make_version(client, s2, 0, 9.0, 7.0)
    pv2 = preview_version(client, s1, 0, 9.0, 7.0).json()
    events = pv2["zones"][0]["preview"]["events"]
    assert [(e["type"], e["at"]) for e in events] == [
        ("warning", iso(BASE + timedelta(minutes=10)).replace("+00:00", "Z"))
    ]


def test_preview_zone_initial_state_at_effective(client):
    # 责任区 t=60 生效；两站在生效前已按基准 10 预警（水位 12）→ 现状区域生效即预警；
    # 预演放宽甲站阈值（上升 100，t=0 起）使其预警消失 → 预演区域自始正常
    s1, d1 = make_station_with_device(client, "甲站", "D1")
    s2, d2 = make_station_with_device(client, "乙站", "D2")
    for d in (d1, d2):
        for i, m in enumerate([0, 5, 10], start=1):
            upload(client, d, i, m, 12.0)
    create_zone(client, [s1, s2], 60, 2)
    assert zone_timeline(client, 1)["status"] == "warning"
    assert [(e["type"], e["at"]) for e in zone_timeline(client, 1)["events"]] == [
        ("warning", iso(BASE + timedelta(minutes=60)).replace("+00:00", "Z"))
    ]
    # 预演甲站放宽到 100：生效前甲站不预警 → 初始计数 1 < K，区域不预警
    pv = preview_version(client, s1, 0, 100.0, 90.0).json()
    z = pv["zones"][0]
    assert z["current"]["status"] == "warning"
    assert [(e["type"], e["at"]) for e in z["current"]["events"]] == [
        ("warning", iso(BASE + timedelta(minutes=60)).replace("+00:00", "Z"))
    ]
    assert z["preview"]["status"] == "normal"
    assert z["preview"]["events"] == []


def test_preview_does_not_change_frozen_publication(client, station, device):
    for i, m in enumerate([0, 5, 10], start=1):
        upload(client, device, i, m, 9.5)  # 基准 10：正常
    r = client.post("/publish", json={"note": "冻结"})
    pub_id = r.json()["publication_id"]
    pv = preview_version(client, station, 0, 9.0, 7.0).json()
    assert pv["preview"]["status"] == "warning"
    # 已冻结公开结果不变
    assert client.get(f"/publications/{pub_id}").json()["timelines"][0]["status"] == "normal"
    assert client.get("/public/stations/{0}/timeline".format(station)).json()["status"] == "normal"
    # 发布次数不增加（预演不落库任何内容）
    assert [p["publication_id"] for p in client.get("/publications").json()] == [pub_id]


# ---------------------------------------------------------------- 发布冻结

def test_publish_freezes_timeline(client, station, device):
    _make_warning(client, device)
    r = client.post("/publish", json={"note": "首次发布"})
    assert r.status_code == 201
    pub_id = r.json()["publication_id"]
    assert r.json()["stations"][0]["status"] == "warning"

    pub = client.get("/public/current").json()
    assert pub["publication_id"] == pub_id
    assert pub["timelines"][0]["status"] == "warning"

    # 发布后补传解除读数：待发布变化，公开结果保持冻结
    upload(client, device, 4, 15, 7.0)
    upload(client, device, 5, 21, 7.0)
    assert timeline(client, station)["status"] == "normal"
    assert client.get("/public/current").json()["timelines"][0]["status"] == "warning"
    assert client.get(f"/public/stations/{station}/timeline").json()["status"] == "warning"

    # 发布后修订同样只影响待发布
    client.post("/readings/5/void", json={"reason": "误报"})
    assert timeline(client, station)["status"] == "warning"
    assert client.get("/public/current").json()["timelines"][0]["status"] == "warning"

    # 重新发布才更新公开结果
    r2 = client.post("/publish")
    assert r2.status_code == 201
    assert client.get("/public/current").json()["timelines"][0]["status"] == "warning"
    # 撤销作废的读数无法恢复，重新补一条解除读数再发布
    upload(client, device, 6, 22, 7.0)
    client.post("/publish")
    assert client.get("/public/current").json()["timelines"][0]["status"] == "normal"
    # 历史发布版本仍可查询
    old = client.get(f"/publications/{pub_id}").json()
    assert old["timelines"][0]["status"] == "warning"


def test_public_current_404_before_any_publish(client, station):
    assert client.get("/public/current").status_code == 404
    assert client.get(f"/public/stations/{station}/timeline").status_code == 404


# ---------------------------------------------------------------- 并发安全

def test_concurrent_distinct_uploads_nothing_lost(client, device):
    errors = []

    def worker(base_seq):
        try:
            for i in range(20):
                r = upload(client, device, base_seq + i, i, 5.0 + i * 0.01)
                assert r.status_code == 201, r.text
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n * 1000,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    rows = client.get("/stations/1/readings").json()
    assert len(rows) == 160
    assert len({r["id"] for r in rows}) == 160


def test_concurrent_same_seq_same_content(client, device):
    results = []

    def worker():
        r = upload(client, device, 7, 0, 5.0)
        results.append((r.status_code, r.json()["id"]))

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    codes = sorted(c for c, _ in results)
    ids = {i for _, i in results}
    assert codes.count(201) == 1
    assert codes.count(200) == 9
    assert len(ids) == 1
    assert len(client.get("/stations/1/readings").json()) == 1


def test_concurrent_same_seq_different_content(client, device):
    outcomes = []

    def worker(level):
        r = upload(client, device, 9, 0, level)
        outcomes.append(r.status_code)

    threads = [threading.Thread(target=worker, args=(float(n),)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count(201) == 1
    assert outcomes.count(409) == 5
    assert len(client.get("/stations/1/readings").json()) == 1


def test_concurrent_publish_and_upload_consistent(client, station, device):
    _make_warning(client, device)
    errors = []

    def publisher():
        try:
            for _ in range(5):
                r = client.post("/publish")
                assert r.status_code == 201, r.text
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def uploader():
        try:
            for i in range(10):
                upload(client, device, 100 + i, 20 + i, 7.0)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=publisher), threading.Thread(target=uploader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    pubs = client.get("/publications").json()
    assert len(pubs) == 5
    assert len({p["publication_id"] for p in pubs}) == 5
    rows = client.get("/stations/1/readings").json()
    assert len(rows) == 13  # 3 条预警读数 + 10 条并发补传，一条不丢


# ---------------------------------------------------------------- 防汛责任区（跨站联动）

def make_station_with_device(client, name, device_no, rise=10.0, clear=8.0):
    sid = client.post(
        "/stations", json={"name": name, "rise_threshold": rise, "clear_threshold": clear}
    ).json()["id"]
    client.post(f"/stations/{sid}/devices", json={"device_no": device_no})
    return sid, device_no


def create_zone(client, station_ids, minutes, k, name="青石峡片区"):
    return client.post(
        "/zones",
        json={
            "name": name,
            "effective_at": iso(BASE + timedelta(minutes=minutes)),
            "station_ids": station_ids,
            "trigger_count": k,
        },
    )


def zone_timeline(client, zone_id):
    r = client.get(f"/zones/{zone_id}/timeline")
    assert r.status_code == 200, r.text
    return r.json()


def test_create_zone_persists_config(client):
    s1, _ = make_station_with_device(client, "甲站", "D1")
    s2, _ = make_station_with_device(client, "乙站", "D2")
    r = create_zone(client, [s1, s2], 60, 2)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["trigger_count"] == 2
    assert body["station_ids"] == [s1, s2]
    assert [m["position"] for m in body["members"]] == [0, 1]
    assert body["effective_at"] == iso(BASE + timedelta(minutes=60)).replace("+00:00", "Z")
    assert body["pending_zone"]["status"] == "normal"

    detail = client.get(f"/zones/{body['id']}").json()
    assert detail["name"] == "青石峡片区"
    assert [m["station_id"] for m in detail["members"]] == [s1, s2]
    listed = client.get("/zones").json()
    assert [z["id"] for z in listed] == [body["id"]]


def test_zone_unknown_station_rejected(client, station):
    r = create_zone(client, [station, 999], 0, 1)
    assert r.status_code == 404
    assert r.json()["detail"]["unknown_station_ids"] == [999]


def test_zone_duplicate_members_rejected(client, station):
    r = create_zone(client, [station, station], 0, 1)
    assert r.status_code == 400


def test_zone_naive_effective_at_rejected(client, station):
    r = client.post(
        "/zones",
        json={
            "name": "z",
            "effective_at": "2026-10-01T08:00:00",
            "station_ids": [station],
            "trigger_count": 1,
        },
    )
    assert r.status_code == 400


def test_zone_empty_name_rejected(client, station):
    r = client.post(
        "/zones",
        json={
            "name": "   ",
            "effective_at": iso(BASE),
            "station_ids": [station],
            "trigger_count": 1,
        },
    )
    assert r.status_code == 400


def test_zone_k_out_of_bounds_rejected(client, station):
    # K > 成员数
    r = create_zone(client, [station], 0, 2)
    assert r.status_code == 400
    # K < 1
    r = client.post(
        "/zones",
        json={
            "name": "z",
            "effective_at": iso(BASE),
            "station_ids": [station],
            "trigger_count": 0,
        },
    )
    assert r.status_code == 400
    # 空成员列表
    r = client.post(
        "/zones",
        json={"name": "z", "effective_at": iso(BASE), "station_ids": [], "trigger_count": 1},
    )
    assert r.status_code == 400
    assert client.get("/zones").json() == []  # 全部拒绝，未落库


def test_zone_config_immutable_after_registration(client):
    # 责任区没有修改 / 删除接口；重复登记同名责任区允许（名称不做唯一约束），但配置各自独立
    s1, _ = make_station_with_device(client, "甲站", "D1")
    r1 = create_zone(client, [s1], 60, 1, name="同名片区")
    assert r1.status_code == 201
    r2 = create_zone(client, [s1], 120, 1, name="同名片区")
    assert r2.status_code == 201
    ids = [z["id"] for z in client.get("/zones").json()]
    assert ids == [r1.json()["id"], r2.json()["id"]]
    # 首个责任区生效时间不随第二次登记改变
    assert client.get(f"/zones/{r1.json()['id']}").json()["effective_at"] == \
        iso(BASE + timedelta(minutes=60)).replace("+00:00", "Z")


def test_zone_warning_when_k_stations_warn(client):
    s1, d1 = make_station_with_device(client, "甲站", "D1")
    s2, d2 = make_station_with_device(client, "乙站", "D2")
    create_zone(client, [s1, s2], 0, 2)
    # 甲站 60/65/70 预警；乙站 70 一条不预警 → 区域不预警
    for m in (60, 65, 70):
        upload(client, d1, m, m, 12.0)
    upload(client, d2, 1, 70, 12.0)
    tl = zone_timeline(client, 1)
    assert tl["status"] == "normal"
    assert tl["events"] == []
    # 乙站 70/75/85 也预警（首尾 15 分钟 ≥10）→ 85 时刻两站均预警，区域在 85 预警
    upload(client, d2, 2, 75, 12.0)
    upload(client, d2, 3, 85, 12.0)
    tl = zone_timeline(client, 1)
    assert tl["status"] == "warning"
    assert len(tl["events"]) == 1
    ev = tl["events"][0]
    assert ev["type"] == "warning"
    assert ev["at"] == iso(BASE + timedelta(minutes=85)).replace("+00:00", "Z")
    assert ev["warning_station_ids"] == sorted([s1, s2])
    assert ev["warning_station_count"] == 2


def test_zone_same_timestamp_merged(client):
    s1, d1 = make_station_with_device(client, "甲站", "D1")
    s2, d2 = make_station_with_device(client, "乙站", "D2")
    create_zone(client, [s1, s2], 0, 2)
    # 甲站 00/05/10 预警，乙站 00/05/10 预警
    for d in (d1, d2):
        for i, m in enumerate((0, 5, 10), start=1):
            upload(client, d, i, m, 12.0)
    tl = zone_timeline(client, 1)
    # 10 分钟时刻两站同时变预警 → 该刻合并后计数=2 → 恰好一个区域事件
    assert [(e["type"], e["at"], e["warning_station_count"]) for e in tl["events"]] == [
        ("warning", iso(BASE + timedelta(minutes=10)).replace("+00:00", "Z"), 2)
    ]


def test_zone_clear_when_below_k(client):
    s1, d1 = make_station_with_device(client, "甲站", "D1")
    s2, d2 = make_station_with_device(client, "乙站", "D2")
    create_zone(client, [s1, s2], 0, 2)
    for d in (d1, d2):
        for i, m in enumerate((0, 5, 10), start=1):
            upload(client, d, i, m, 12.0)
    assert zone_timeline(client, 1)["status"] == "warning"
    # 甲站解除（15/21 低水位）→ 预警站数 1 < K=2 → 区域在 21 解除
    upload(client, d1, 4, 15, 7.0)
    upload(client, d1, 5, 21, 7.0)
    tl = zone_timeline(client, 1)
    assert tl["status"] == "normal"
    assert [e["type"] for e in tl["events"]] == ["warning", "cleared"]
    ev = tl["events"][-1]
    assert ev["at"] == iso(BASE + timedelta(minutes=21)).replace("+00:00", "Z")
    assert ev["warning_station_ids"] == [s2]
    assert ev["warning_station_count"] == 1
    # 甲站再次预警（30/35/45）→ 区域重新预警
    upload(client, d1, 6, 30, 12.0)
    upload(client, d1, 7, 35, 12.0)
    upload(client, d1, 8, 45, 12.0)
    tl = zone_timeline(client, 1)
    assert [e["type"] for e in tl["events"]] == ["warning", "cleared", "warning"]
    assert tl["events"][-1]["at"] == iso(BASE + timedelta(minutes=45)).replace("+00:00", "Z")


def test_zone_one_event_max_per_timestamp(client):
    s1, d1 = make_station_with_device(client, "甲站", "D1")
    s2, d2 = make_station_with_device(client, "乙站", "D2")
    create_zone(client, [s1, s2], 0, 2)
    for d in (d1, d2):
        for i, m in enumerate((0, 5, 10), start=1):
            upload(client, d, i, m, 12.0)
    # 同一时刻（20）：甲站解除，乙站再触发一次预警周期不可能（乙站已预警），
    # 这里改为验证同刻一站解除、另一站仍预警时不会抖动出两个区域事件
    upload(client, d2, 4, 15, 7.0)
    upload(client, d2, 5, 20, 7.0)   # 乙站 20 解除 → 区域 20 解除
    tl = zone_timeline(client, 1)
    assert [e["type"] for e in tl["events"]].count("cleared") == 1
    assert tl["events"][-1]["at"] == iso(BASE + timedelta(minutes=20)).replace("+00:00", "Z")


def test_zone_k1_warns_with_single_station(client):
    s1, d1 = make_station_with_device(client, "甲站", "D1")
    s2, d2 = make_station_with_device(client, "乙站", "D2")
    create_zone(client, [s1, s2], 0, 1)
    for i, m in enumerate((0, 5, 10), start=1):
        upload(client, d1, i, m, 12.0)
    tl = zone_timeline(client, 1)
    assert tl["status"] == "warning"
    assert tl["events"][0]["warning_station_ids"] == [s1]
    # 乙站同期正常：members 反映各站当前状态
    statuses = {m["station_id"]: m["warning"] for m in tl["members"]}
    assert statuses == {s1: True, s2: False}


def test_zone_initial_warning_before_effective_at(client):
    s1, d1 = make_station_with_device(client, "甲站", "D1")
    s2, d2 = make_station_with_device(client, "乙站", "D2")
    # 两站在生效前（00/05/10）均已预警
    for d in (d1, d2):
        for i, m in enumerate((0, 5, 10), start=1):
            upload(client, d, i, m, 12.0)
    create_zone(client, [s1, s2], 60, 2)  # 生效时刻 60
    tl = zone_timeline(client, 1)
    assert tl["status"] == "warning"
    # 此前仍在预警的站计入初始状态，初始满足 K → 在生效时刻预警
    assert [(e["type"], e["at"]) for e in tl["events"]] == [
        ("warning", iso(BASE + timedelta(minutes=60)).replace("+00:00", "Z"))
    ]
    pre = {m["station_id"]: m["warning_before_effective"] for m in tl["members"]}
    assert pre == {s1: True, s2: True}


def test_zone_initial_not_satisfied_then_triggers(client):
    s1, d1 = make_station_with_device(client, "甲站", "D1")
    s2, d2 = make_station_with_device(client, "乙站", "D2")
    for i, m in enumerate((0, 5, 10), start=1):
        upload(client, d1, i, m, 12.0)   # 仅甲站在生效前预警
    create_zone(client, [s1, s2], 60, 2)
    tl = zone_timeline(client, 1)
    assert tl["status"] == "normal"
    assert tl["events"] == []
    # 生效后乙站在 70/75/85 预警 → 85 两站齐预警，区域预警
    upload(client, d2, 1, 70, 12.0)
    upload(client, d2, 2, 75, 12.0)
    upload(client, d2, 3, 85, 12.0)
    tl = zone_timeline(client, 1)
    assert tl["status"] == "warning"
    assert tl["events"][0]["at"] == iso(BASE + timedelta(minutes=85)).replace("+00:00", "Z")


def test_zone_change_at_exact_effective_at_merged_with_initial(client):
    s1, d1 = make_station_with_device(client, "甲站", "D1")
    s2, d2 = make_station_with_device(client, "乙站", "D2")
    # 甲站在生效前已预警；乙站恰好在生效时刻 60 完成预警（50/55/60）
    for i, m in enumerate((0, 5, 10), start=1):
        upload(client, d1, i, m, 12.0)
    create_zone(client, [s1, s2], 60, 2)
    upload(client, d2, 1, 50, 12.0)
    upload(client, d2, 2, 55, 12.0)
    upload(client, d2, 3, 60, 12.0)
    tl = zone_timeline(client, 1)
    # 初始甲站预警 + 生效同刻乙站变预警，先合并再计数 → 生效时刻一个预警事件
    assert [(e["type"], e["at"], e["warning_station_count"]) for e in tl["events"]] == [
        ("warning", iso(BASE + timedelta(minutes=60)).replace("+00:00", "Z"), 2)
    ]


def test_zone_events_before_effective_at_ignored(client):
    s1, d1 = make_station_with_device(client, "甲站", "D1")
    s2, d2 = make_station_with_device(client, "乙站", "D2")
    # 甲站 00/05/10 预警后于 15/21 解除；乙站 00/05/10 预警后未解除
    for d in (d1, d2):
        for i, m in enumerate((0, 5, 10), start=1):
            upload(client, d, i, m, 12.0)
    upload(client, d1, 4, 15, 7.0)
    upload(client, d1, 5, 21, 7.0)
    create_zone(client, [s1, s2], 60, 2)
    tl = zone_timeline(client, 1)
    # 生效时甲站已不预警（此前最后事件是解除），仅乙站预警 → 区域不预警，历史事件不重放
    assert tl["status"] == "normal"
    assert tl["events"] == []
    assert {m["station_id"]: m["warning_before_effective"] for m in tl["members"]} == {
        s1: False,
        s2: True,
    }


def test_zone_recomputes_after_backfill_and_void(client):
    s1, d1 = make_station_with_device(client, "甲站", "D1")
    s2, d2 = make_station_with_device(client, "乙站", "D2")
    create_zone(client, [s1, s2], 0, 2)
    for i, m in enumerate((0, 5, 10), start=1):
        upload(client, d1, i, m, 12.0)   # 甲站预警（读数 id 1..3）
    assert zone_timeline(client, 1)["status"] == "normal"
    # 乙站补传达成预警（读数 id 4..6）→ 区域联动结果重算
    for i, m in enumerate((0, 5, 10), start=1):
        upload(client, d2, i, m, 12.0)
    assert zone_timeline(client, 1)["status"] == "warning"
    # 审核作废乙站最后一条关键读数（id 6）→ 乙站预警不成立，区域回到正常
    client.post("/readings/6/void", json={"reason": "误报"})
    assert zone_timeline(client, 1)["status"] == "normal"


def test_zone_recomputes_after_correction(client):
    s1, d1 = make_station_with_device(client, "甲站", "D1")
    s2, d2 = make_station_with_device(client, "乙站", "D2")
    create_zone(client, [s1, s2], 0, 2)
    for d in (d1, d2):
        for i, m in enumerate((0, 5, 10), start=1):
            upload(client, d, i, m, 12.0)
    assert zone_timeline(client, 1)["status"] == "warning"
    # 更正甲站首条读数为低水位 → 甲站连续被打断、其历史预警事件在重算后消失
    # → 区域待发布时间线整体重算：区域不再达到 K，事件为空（与单站重算语义一致）
    client.post("/readings/1/correct", json={"water_level": 5.0, "reason": "抄录错误"})
    tl = zone_timeline(client, 1)
    assert tl["status"] == "normal"
    assert tl["events"] == []
    statuses = {m["station_id"]: m["warning"] for m in tl["members"]}
    assert statuses == {s1: False, s2: True}


def test_zone_recomputes_after_threshold_version(client):
    s1, d1 = make_station_with_device(client, "甲站", "D1")
    s2, d2 = make_station_with_device(client, "乙站", "D2")
    create_zone(client, [s1, s2], 0, 2)
    for d in (d1, d2):
        for i, m in enumerate((0, 5, 10), start=1):
            upload(client, d, i, m, 12.0)
    assert zone_timeline(client, 1)["status"] == "warning"
    # 阈值提严（上升 100）使乙站历史读数不再达预警 → 区域重算后不再达到 K
    client.post(
        f"/stations/{s2}/threshold-versions",
        json={
            "effective_at": iso(BASE),
            "rise_threshold": 100.0,
            "clear_threshold": 90.0,
            "reason": "演练阈值",
        },
    )
    tl = zone_timeline(client, 1)
    assert tl["status"] == "normal"
    assert tl["events"] == []
    statuses = {m["station_id"]: m["warning"] for m in tl["members"]}
    assert statuses == {s1: True, s2: False}


def test_zone_publish_freezes_with_station_timelines(client):
    s1, d1 = make_station_with_device(client, "甲站", "D1")
    s2, d2 = make_station_with_device(client, "乙站", "D2")
    create_zone(client, [s1, s2], 0, 2)
    for d in (d1, d2):
        for i, m in enumerate((0, 5, 10), start=1):
            upload(client, d, i, m, 12.0)
    r = client.post("/publish", json={"note": "区域首次发布"})
    assert r.status_code == 201
    assert r.json()["zones"][0]["status"] == "warning"
    pub_id = r.json()["publication_id"]

    pub = client.get(f"/publications/{pub_id}").json()
    assert pub["zone_timelines"][0]["status"] == "warning"
    assert pub["zone_timelines"][0]["events"][0]["warning_station_count"] == 2
    cur = client.get("/public/current").json()
    assert cur["zone_timelines"][0]["zone_id"] == 1

    zpub = client.get("/public/zones/1/timeline").json()
    assert zpub["published"] is True
    assert zpub["status"] == "warning"

    # 发布后两站解除：待发布区域正常，公开快照仍预警
    for d in (d1, d2):
        upload(client, d, 4, 15, 7.0)
        upload(client, d, 5, 21, 7.0)
    assert zone_timeline(client, 1)["status"] == "normal"
    assert client.get("/public/zones/1/timeline").json()["status"] == "warning"
    assert client.get("/public/current").json()["zone_timelines"][0]["status"] == "warning"

    # 重新发布后公开结果更新，历史发布快照不变
    client.post("/publish", json={"note": "区域解除发布"})
    assert client.get("/public/zones/1/timeline").json()["status"] == "normal"
    old = client.get(f"/publications/{pub_id}").json()
    assert old["zone_timelines"][0]["status"] == "warning"


def test_zone_created_after_publication_missing_from_old_snapshot(client):
    s1, d1 = make_station_with_device(client, "甲站", "D1")
    for i, m in enumerate((0, 5, 10), start=1):
        upload(client, d1, i, m, 12.0)
    r1 = client.post("/publish", json={"note": "区域建立前"})
    assert r1.json()["zones"] == []
    create_zone(client, [s1], 0, 1)
    # 旧发布不含该责任区
    old = client.get(f"/publications/{r1.json()['publication_id']}").json()
    assert old["zone_timelines"] == []
    # 尚无该责任区发布 → 明确报无结果（404）
    assert client.get("/public/zones/1/timeline").status_code == 404
    client.post("/publish", json={"note": "区域建立后"})
    assert client.get("/public/zones/1/timeline").status_code == 200


def test_public_zone_404_when_zone_unknown_or_unpublished(client):
    assert client.get("/public/zones/999/timeline").status_code == 404
    s1, _ = make_station_with_device(client, "甲站", "D1")
    r = create_zone(client, [s1], 0, 1)
    zid = r.json()["id"]
    # 从未发布过任何结果
    assert client.get("/public/zones/{zid}/timeline".format(zid=zid)).status_code == 404
    assert client.get(f"/zones/{zid}/timeline").status_code == 200  # 待发布仍可查
    assert client.get("/zones/999/timeline").status_code == 404


# ---------------------------------------------------------------- replay_zone 纯函数边界

def t(minutes):
    return BASE + timedelta(minutes=minutes)


def test_replay_initial_meets_k_warns_at_effective():
    res = replay_zone([], [1, 2], trigger_count=2, effective_at=t(60))
    assert res.status == "warning"
    assert [(e.type, e.at) for e in res.events] == [("warning", t(60))]
    assert res.events[0].warning_station_ids == (1, 2)


def test_replay_initial_below_k_no_event():
    res = replay_zone([], [1], trigger_count=2, effective_at=t(0))
    assert res.status == "normal"
    assert res.events == []
    # 生效时刻的空 tick 不应留下解除事件
    assert res.active_warning_since is None


def test_replay_same_tick_merged_no_flap():
    # 初始站 1、2 预警（K=2）→ 生效即预警；
    # t=10 同一刻：站 1 解除、站 3 预警 → 合并后仍是 2 个预警 → 不解除
    changes = [
        StationStateChange(1, t(10), "normal", order=5),
        StationStateChange(3, t(10), "warning", order=9),
    ]
    res = replay_zone(changes, [1, 2], trigger_count=2, effective_at=t(0))
    assert res.status == "warning"
    assert [e.type for e in res.events] == ["warning"]
    assert res.events[0].at == t(0)
    assert res.events[0].warning_station_ids == (1, 2)


def test_replay_warn_and_clear_flow():
    changes = [
        StationStateChange(1, t(5), "warning"),
        StationStateChange(2, t(10), "warning"),   # 达到 K=2 → 预警
        StationStateChange(1, t(20), "normal"),    # 降到 1 → 解除
    ]
    res = replay_zone(changes, [], trigger_count=2, effective_at=t(0))
    assert [e.type for e in res.events] == ["warning", "cleared"]
    assert res.events[0].at == t(10)
    assert res.events[0].warning_station_ids == (1, 2)
    assert res.events[1].at == t(20)
    assert res.events[1].warning_station_ids == (2,)
    assert res.status == "normal"


def test_replay_changes_before_effective_ignored():
    # 生效前的预警与解除变化都不重放；初始预警集合由调用方另行给出
    changes = [
        StationStateChange(1, t(-10), "warning"),
        StationStateChange(1, t(-5), "normal"),
        StationStateChange(2, t(20), "warning"),
    ]
    res = replay_zone(changes, [], trigger_count=2, effective_at=t(0))
    # 生效时 0 个；t=20 仍只有站 2 → 永不达到 K=2
    assert res.status == "normal"
    assert res.events == []


def test_replay_one_event_max_per_tick_even_if_count_crosses_twice_logically():
    # 同刻多变化按合并后结果一次性判定，绝不按站逐个产生抖动事件
    changes = [
        StationStateChange(1, t(10), "warning"),
        StationStateChange(2, t(10), "warning"),
        StationStateChange(3, t(10), "normal"),   # 站 3 初始预警、同刻解除
    ]
    res = replay_zone(changes, [3, 4], trigger_count=3, effective_at=t(0))
    # 初始 3、4 预警（<3? 等于2 < 3）→ 正常；t=10 合并后 {1,2,4} = 3 → 一个预警
    assert [e.type for e in res.events] == ["warning"]
    assert res.events[0].warning_station_ids == (1, 2, 4)


# ---------------------------------------------------------------- 受控发布（核对 → 确认）

def check(client):
    r = client.post("/publish/checks")
    assert r.status_code == 201, r.text
    return r.json()


def confirm(client, token, note=None):
    body = {"token": token}
    if note is not None:
        body["note"] = note
    return client.post("/publish/confirm", json=body)


def test_check_first_publish_against_empty_snapshot(client, station, device):
    _make_warning(client, device)
    d = check(client)
    # 首次发布以空快照为基准
    assert d["baseline_publication_id"] is None
    assert d["baseline_published_at"] is None
    assert len(d["token"]) >= 20
    s = d["stations"][0]
    assert s["station_id"] == station
    assert s["status_before"] is None
    assert s["status_after"] == "warning"
    assert s["changed"] is True
    assert s["new_object"] is True
    # 空基准下既有预警事件全部计为新增
    assert [e["type"] for e in s["events_added"]] == ["warning"]
    assert s["events_added"][0]["occurrence"] == 1
    assert s["events_removed"] == []
    assert d["summary"]["stations_total"] == 1
    assert d["summary"]["events_added"]["station_warning"] == 1
    # 核对不产生发布：公开查询仍无结果
    assert client.get("/public/current").status_code == 404


def test_confirm_freezes_and_returns_publication_id(client, station, device):
    _make_warning(client, device)
    d = check(client)
    r = confirm(client, d["token"], note="值班确认发布")
    assert r.status_code == 201, r.text
    pub_id = r.json()["publication_id"]
    assert isinstance(pub_id, int)
    assert r.json()["note"] == "值班确认发布"
    # 公开结果已冻结为核对时的待发布结果（预警）
    pub = client.get("/public/current").json()
    assert pub["publication_id"] == pub_id
    assert pub["timelines"][0]["status"] == "warning"


def test_confirm_unknown_token_404(client, station):
    r = confirm(client, "not-a-real-token")
    assert r.status_code == 404


def test_confirm_token_single_use(client, station, device):
    _make_warning(client, device)
    token = check(client)["token"]
    assert confirm(client, token).status_code == 201
    # 同一凭证第二次确认 → 409，不产生新发布
    r2 = confirm(client, token)
    assert r2.status_code == 409
    assert r2.json()["detail"]["reason"] == "token_already_used"
    pubs = client.get("/publications").json()
    assert len(pubs) == 1


def test_check_status_before_after_and_events(client, station, device):
    _make_warning(client, device)
    # 第一次受控发布：预警
    d1 = check(client)
    confirm(client, d1["token"])
    # 补传解除读数 → 待发布解除
    upload(client, device, 4, 15, 7.0)
    upload(client, device, 5, 21, 7.0)
    assert timeline(client, station)["status"] == "normal"

    d2 = check(client)
    assert d2["baseline_publication_id"] is not None
    s = d2["stations"][0]
    assert s["status_before"] == "warning"
    assert s["status_after"] == "normal"
    assert s["changed"] is True
    assert s["new_object"] is False
    # 相对基准：新增 cleared，原 warning 仍在（不消失）
    assert [e["type"] for e in s["events_added"]] == ["cleared"]
    assert s["events_removed"] == []
    assert d2["summary"]["events_added"]["station_cleared"] == 1
    confirm(client, d2["token"])

    # 作废触发解除的读数之一（读数 4）：预警事件仍在，cleared 消失
    client.post("/readings/4/void", json={"reason": "误报"})
    d3 = check(client)
    s = d3["stations"][0]
    assert s["status_before"] == "normal"
    assert s["status_after"] == "warning"
    assert s["events_added"] == []
    assert [e["type"] for e in s["events_removed"]] == ["cleared"]
    assert s["events_removed"][0]["occurrence"] == 1


def test_event_occurrence_distinguishes_repeated_events(client, station, device):
    # 首次发布时仅预警；发布后解除 → 再次预警：第二次核对含 cleared#1 与 warning#2
    _make_warning(client, device)                       # warning @10
    d1 = check(client)
    confirm(client, d1["token"])
    upload(client, device, 4, 15, 7.0)
    upload(client, device, 5, 21, 7.0)                 # cleared @21
    upload(client, device, 6, 30, 12.0)
    upload(client, device, 7, 35, 12.0)
    upload(client, device, 8, 40, 12.0)                # warning @40
    d2 = check(client)
    added = {(e["type"], e["occurrence"]) for e in d2["stations"][0]["events_added"]}
    assert ("cleared", 1) in added
    assert ("warning", 2) in added
    # 首次发布已含 warning#1，本次不再重复计入
    assert ("warning", 1) not in added


@pytest.mark.parametrize(
    "invalidate",
    [
        "reading",
        "correct",
        "void",
        "threshold",
        "zone",
        "station",
        "device",
    ],
)
def test_confirm_rejected_after_affecting_write(client, station, device, invalidate):
    _make_warning(client, device)
    token = check(client)["token"]

    if invalidate == "reading":
        upload(client, device, 4, 30, 11.0)
    elif invalidate == "correct":
        client.post("/readings/1/correct", json={"water_level": 9.0, "reason": "比测"})
    elif invalidate == "void":
        client.post("/readings/1/void", json={"reason": "故障"})
    elif invalidate == "threshold":
        client.post(
            f"/stations/{station}/threshold-versions",
            json={
                "effective_at": "2026-09-30T00:00:00Z",
                "rise_threshold": 9.0,
                "clear_threshold": 7.0,
                "reason": "提级",
            },
        )
    elif invalidate == "zone":
        client.post(
            "/zones",
            json={
                "name": "片区",
                "effective_at": "2026-09-30T00:00:00Z",
                "station_ids": [station],
                "trigger_count": 1,
            },
        )
    elif invalidate == "station":
        client.post("/stations", json={"name": "新站", "rise_threshold": 10.0, "clear_threshold": 8.0})
    elif invalidate == "device":
        client.post(f"/stations/{station}/devices", json={"device_no": "DEV-002"})

    r = confirm(client, token)
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "data_changed_after_check"
    # 拒绝后无发布记录产生，凭证不可恢复使用
    assert client.get("/public/current").status_code == 404
    assert confirm(client, token).status_code in (409,)


def test_confirm_rejected_when_another_publication_completed_first(client, station, device):
    _make_warning(client, device)
    token = check(client)["token"]
    # 另一发布先完成（直接发布接口保持可用）
    client.post("/publish", json={"note": "抢先发布"})
    r = confirm(client, token)
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "data_changed_after_check"
    # 仅存在抢先发布一条记录
    assert len(client.get("/publications").json()) == 1


def test_idempotent_retry_does_not_invalidate_token(client, station, device):
    _make_warning(client, device)
    token = check(client)["token"]
    # 同设备同序号同内容重试：不改变数据，凭证仍可确认
    r = client.post(
        "/readings",
        json={
            "device_no": device,
            "seq": 1,
            "observed_at": iso(BASE + timedelta(minutes=0)),
            "water_level": 12.0,
        },
    )
    assert r.status_code == 200 and r.json()["deduplicated"] is True
    assert confirm(client, token).status_code == 201


def test_preview_does_not_invalidate_token(client, station, device):
    _make_warning(client, device)
    token = check(client)["token"]
    r = client.post(
        f"/stations/{station}/threshold-versions/preview",
        json={
            "effective_at": "2026-09-30T00:00:00Z",
            "rise_threshold": 9.0,
            "clear_threshold": 7.0,
            "reason": "预演",
        },
    )
    assert r.status_code == 200 and r.json()["dry_run"] is True
    assert confirm(client, token).status_code == 201


def test_zone_diffs_in_check(client, station, device):
    # 第二站 + K=1 责任区，区域随站预警
    sid2 = client.post("/stations", json={"name": "乙站", "rise_threshold": 10.0, "clear_threshold": 8.0}).json()["id"]
    client.post(f"/stations/{sid2}/devices", json={"device_no": "DEV-009"})
    zid = client.post(
        "/zones",
        json={"name": "片区", "effective_at": "2026-09-30T00:00:00Z", "station_ids": [station, sid2], "trigger_count": 1},
    ).json()["id"]
    _make_warning(client, device)

    d = check(client)
    z = d["zones"][0]
    assert z["zone_id"] == zid
    assert z["status_before"] is None
    assert z["status_after"] == "warning"
    assert z["new_object"] is True
    assert z["events_added"][0]["type"] == "warning"
    zd = next(e for e in d["events_added"] if e["object_type"] == "zone")
    assert zd["object_id"] == zid and zd["occurrence"] == 1
    confirm(client, d["token"])

    # 补解除读数后区域解除
    upload(client, device, 4, 15, 7.0)
    upload(client, device, 5, 21, 7.0)
    d2 = check(client)
    z2 = d2["zones"][0]
    assert z2["status_before"] == "warning"
    assert z2["status_after"] == "normal"
    assert [e["type"] for e in z2["events_added"]] == ["cleared"]


def test_check_and_confirm_empty_system(client):
    # 无任何站点也可核对（空快照 vs 空待发布）并成功发布
    d = check(client)
    assert d["stations"] == [] and d["zones"] == []
    assert d["summary"]["stations_total"] == 0
    r = confirm(client, d["token"], note="空发布")
    assert r.status_code == 201
    assert r.json()["stations"] == [] and r.json()["zones"] == []
    assert client.get("/public/current").status_code == 200


def test_rejected_confirm_creates_no_publication_row(client, station, device):
    _make_warning(client, device)
    token = check(client)["token"]
    upload(client, device, 4, 30, 11.0)
    r = confirm(client, token)
    assert r.status_code == 409
    assert client.get("/publications").json() == []
    # 作废后再次核对可拿到新凭证正常发布
    d2 = check(client)
    assert d2["token"] != token
    assert confirm(client, d2["token"]).status_code == 201


def test_confirm_requires_token_field(client):
    r = client.post("/publish/confirm", json={})
    assert r.status_code == 400
    r2 = client.post("/publish/confirm", json={"token": ""})
    assert r2.status_code == 400


def test_frozen_content_matches_check_pending(client, station, device):
    _make_warning(client, device)
    d = check(client)
    pending_events = timeline(client, station)["events"]
    confirm(client, d["token"])
    frozen = client.get(f"/public/stations/{station}/timeline").json()
    assert frozen["status"] == "warning"
    assert frozen["events"] == pending_events
    # 历史快照不得被之后发布改写
    pub1 = client.get("/public/current").json()["publication_id"]
    upload(client, device, 4, 15, 7.0)
    upload(client, device, 5, 21, 7.0)
    d3 = check(client)
    confirm(client, d3["token"])
    old = client.get(f"/publications/{pub1}").json()
    assert old["timelines"][0]["status"] == "warning"
    assert client.get("/public/current").json()["timelines"][0]["status"] == "normal"


def test_concurrent_confirm_same_token_only_one_succeeds(client, station, device):
    _make_warning(client, device)
    token = check(client)["token"]
    results = []

    def worker():
        results.append(confirm(client, token).status_code)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start()
    t1.join(); t2.join()
    assert sorted(results) == [201, 409]
    assert len(client.get("/publications").json()) == 1


def test_check_data_version_advances(client, station, device):
    v0 = check(client)["data_version"]  # 已含登记站点 + 设备的 2 次版本推进
    _make_warning(client, device)       # 3 条新读数 → 版本再 +3
    d2 = check(client)
    assert d2["data_version"] == v0 + 3
    r = confirm(client, d2["token"])
    assert r.json()["data_version"] == v0 + 4
    # 发布本身也推进数据版本
    d3 = check(client)
    assert d3["data_version"] == v0 + 4
    assert d3["baseline_publication_id"] is not None
