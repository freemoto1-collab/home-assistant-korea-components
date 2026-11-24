"""한전 시간별 사용량/잉여량을 해당 시각의 외부 통계로 기록한다."""

import re
from datetime import datetime, timedelta

from ..const import DOMAIN, LOGGER

BASE_LOOKBACK = timedelta(days=7)


def statistic_id(unique_id: str, kind: str) -> str:
    """예: korea_incubator:kepco_<고객>_hourly_usage (kind: usage | export)"""
    slug = re.sub(r"[^a-z0-9_]", "_", unique_id.lower())
    return f"{DOMAIN}:{slug}_hourly_{kind}"


def summarize_hourly(points: list[dict]) -> dict:
    """센서용 요약: 가장 최근 집계 시간 값과 오늘 누계."""
    if not points:
        return {}
    last = max(points, key=lambda p: p["start"])
    return {
        "usage": last["usage"],
        "export": last["export"],
        "start": last["start"].isoformat(),
        "end": (last["start"] + timedelta(hours=1)).isoformat(),
        "today_usage": round(sum(p["usage"] for p in points), 6),
        "today_export": round(sum(p["export"] for p in points), 6),
    }


def build_rows(points: list[tuple[datetime, float]], base_sum: float) -> list[dict]:
    """(시작 시각, 값) 목록을 누적 sum/state 행으로 만든다. 같은 시각은 마지막 값."""
    unique = dict(sorted(points, key=lambda p: p[0]))
    total = base_sum
    rows = []
    for start, value in unique.items():
        total = round(total + value, 6)
        rows.append({"start": start, "state": total, "sum": total})
    return rows


def _mean_fields() -> dict:
    """mean_type을 지원하는 HA는 mean_type=NONE, 구버전은 has_mean=False."""
    try:
        from homeassistant.components.recorder.models import StatisticMeanType
    except ImportError:
        return {"has_mean": False}
    return {"mean_type": StatisticMeanType.NONE}


async def _base_sum(hass, stat_id: str, first_start: datetime) -> float:
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        first_start - BASE_LOOKBACK,
        first_start,
        {stat_id},
        "hour",
        None,
        {"sum"},
    )
    rows = [r for r in result.get(stat_id, []) if r.get("sum") is not None]
    if not rows:
        return 0.0
    return float(max(rows, key=lambda r: r["start"])["sum"])


async def async_import_hourly(
    hass,
    unique_id: str,
    name: str,
    points: list[dict],
    kinds: tuple[str, ...] = ("usage", "export"),
) -> None:
    """points: api.async_get_hourly 결과(여러 날 합친 것). 같은 시각은 덮어쓴다."""
    from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
    from homeassistant.components.recorder.statistics import (
        async_add_external_statistics,
    )

    if not points:
        return
    first_start = min(p["start"] for p in points)
    labels = {"usage": "시간별 사용량", "export": "시간별 잉여량"}
    for kind in kinds:
        label = labels[kind]
        stat_id = statistic_id(unique_id, kind)
        base = await _base_sum(hass, stat_id, first_start)
        rows = build_rows([(p["start"], p[kind]) for p in points], base)
        metadata = StatisticMetaData(
            **_mean_fields(),
            has_sum=True,
            name=f"{name} {label}",
            source=DOMAIN,
            statistic_id=stat_id,
            unit_of_measurement="kWh",
            unit_class="energy",
        )
        async_add_external_statistics(
            hass, metadata, [StatisticData(**row) for row in rows]
        )
        LOGGER.debug("KEPCO %s: %d rows imported", stat_id, len(rows))
