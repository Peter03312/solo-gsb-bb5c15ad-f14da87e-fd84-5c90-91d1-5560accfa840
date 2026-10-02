"""服务配置：全部可通过环境变量覆盖。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str
    min_water_level: float
    max_water_level: float
    segment_gap_minutes: int
    rise_min_count: int
    rise_min_span_minutes: int
    clear_min_count: int
    clear_min_span_minutes: int


def load_settings() -> Settings:
    return Settings(
        db_path=os.environ.get("FLOOD_DB_PATH", "./data/flood.db"),
        min_water_level=float(os.environ.get("FLOOD_MIN_WATER_LEVEL", "-50")),
        max_water_level=float(os.environ.get("FLOOD_MAX_WATER_LEVEL", "1000")),
        segment_gap_minutes=int(os.environ.get("FLOOD_SEGMENT_GAP_MINUTES", "15")),
        rise_min_count=int(os.environ.get("FLOOD_RISE_MIN_COUNT", "3")),
        rise_min_span_minutes=int(os.environ.get("FLOOD_RISE_MIN_SPAN_MINUTES", "10")),
        clear_min_count=int(os.environ.get("FLOOD_CLEAR_MIN_COUNT", "2")),
        clear_min_span_minutes=int(os.environ.get("FLOOD_CLEAR_MIN_SPAN_MINUTES", "5")),
    )
