"""KEPCO 시간별 사용량/잉여량 조회 및 외부 통계 테스트 (개인정보 없는 합성 데이터)."""

from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from curl_cffi import AsyncSession

from custom_components.korea_incubator.kepco.api import (
    KST,
    KepcoApiClient,
    parse_hourly_details,
)
from custom_components.korea_incubator.kepco.device import KepcoDevice
from custom_components.korea_incubator.kepco import statistics as stats

DAY = date(2026, 11, 24)


def payload(values):
    """values: [(usage, export)] 를 01시부터 24시까지 채운다(나머지는 0.000)."""
    details = []
    for hour in range(1, 25):
        usage, export = values[hour - 1] if hour <= len(values) else (0, 0)
        details.append(
            {
                "MR_HH": f"{hour:02d}",
                "MR_HHMI": f"{hour:02d}00",
                "F_AP_QT": f"{usage:.3f}",
                "F_AP_QT_REV": f"{export:.3f}",
            }
        )
    return {"SEARCH_DT": "2026-11-24", "details": details}


def test_parse_maps_hour_h_to_interval_start_and_drops_unpublished_tail():
    points = parse_hourly_details(DAY, payload([(0.4, 0), (0.3, 0), (0.2, 0.1)]))
    assert [p["start"].hour for p in points] == [0, 1, 2]
    assert points[0]["start"] == datetime(2026, 11, 24, 0, tzinfo=KST)
    assert points[2]["usage"] == 0.2 and points[2]["export"] == 0.1


def test_parse_keeps_zero_hours_before_last_published():
    points = parse_hourly_details(DAY, payload([(0.4, 0), (0, 0), (0.2, 0)]))
    assert len(points) == 3 and points[1]["usage"] == 0


def test_parse_hour_24_is_last_interval():
    values = [(0.1, 0)] * 24
    points = parse_hourly_details(DAY, payload(values))
    assert points[-1]["start"] == datetime(2026, 11, 24, 23, tzinfo=KST)


def test_parse_empty_or_all_zero():
    assert parse_hourly_details(DAY, {}) == []
    assert parse_hourly_details(DAY, payload([])) == []


@pytest.fixture
def client():
    return KepcoApiClient(MagicMock(spec=AsyncSession))


async def test_api_hourly_methods(client):
    body = payload([(0.4, 0), (0.3, 0.2)])
    with patch.object(client, "_request", AsyncMock(return_value=body)) as req:
        usage = await client.async_get_hourly_usage(DAY)
        export = await client.async_get_hourly_export(DAY)
    assert [v for _, v in usage] == [0.4, 0.3]
    assert [v for _, v in export] == [0.0, 0.2]
    assert req.call_args.args[:2] == (
        "POST",
        "https://pp.kepco.co.kr:8030/low/real/real_day/usage.do",
    )
    assert req.call_args.kwargs["json"] == {"SEARCH_DT": "2026-11-24"}


def test_statistic_id():
    assert (
        stats.statistic_id("kepco_0123", "usage")
        == "korea_incubator:kepco_0123_hourly_usage"
    )
    assert stats.statistic_id("Kepco-A.b", "export").endswith("kepco_a_b_hourly_export")


def test_build_rows_cumulative_and_dedup():
    t0 = datetime(2026, 11, 24, 0, tzinfo=KST)
    rows = stats.build_rows(
        [(t0 + timedelta(hours=1), 2.0), (t0, 1.0), (t0, 1.5)], base_sum=10.0
    )
    assert [r["start"] for r in rows] == [t0, t0 + timedelta(hours=1)]
    assert [r["sum"] for r in rows] == [11.5, 13.5]
    assert rows[0]["state"] == rows[0]["sum"]


async def test_import_overwrites_same_hours_idempotently():
    t0 = datetime(2026, 11, 24, 0, tzinfo=KST)
    points = [
        {"start": t0, "usage": 1.0, "export": 0.0},
        {"start": t0 + timedelta(hours=1), "usage": 2.0, "export": 0.5},
    ]
    calls = []
    with patch.object(stats, "_base_sum", AsyncMock(return_value=5.0)), patch(
        "homeassistant.components.recorder.statistics.async_add_external_statistics",
        side_effect=lambda hass, meta, data: calls.append((meta, list(data))),
    ):
        await stats.async_import_hourly(MagicMock(), "kepco_x", "한전", points)
        await stats.async_import_hourly(MagicMock(), "kepco_x", "한전", points)
    assert len(calls) == 4
    (usage_meta, usage_data), (export_meta, export_data) = calls[0], calls[1]
    assert usage_meta["statistic_id"] == "korea_incubator:kepco_x_hourly_usage"
    assert usage_meta["source"] == "korea_incubator" and usage_meta["has_sum"]
    assert export_meta["statistic_id"].endswith("_hourly_export")
    assert [d["sum"] for d in usage_data] == [6.0, 8.0]
    assert [d["sum"] for d in export_data] == [5.0, 5.5]
    assert calls[2][1] == usage_data and calls[3][1] == export_data
    assert usage_data[0]["start"] == t0


async def test_late_hour_filled_on_later_poll():
    """처음엔 01시 값이 0.000 미집계 → 이후 폴링에서 채워지면 같은 시각에 반영."""
    t0 = datetime(2026, 11, 24, 0, tzinfo=KST)
    first = parse_hourly_details(DAY, payload([(0.0, 0), (0.3, 0)]))
    second = parse_hourly_details(DAY, payload([(0.4, 0), (0.3, 0)]))
    assert [p["usage"] for p in first] == [0.0, 0.3]
    assert [p["usage"] for p in second] == [0.4, 0.3]
    assert [p["start"] for p in second][0] == t0
    assert [r["sum"] for r in stats.build_rows([(p["start"], p["usage"]) for p in second], 0)] == [0.4, 0.7]


@pytest.fixture
def device():
    session = MagicMock(spec=AsyncSession)
    session.close = AsyncMock()
    dev = KepcoDevice(MagicMock(), "e", "test_user", "pw", session)
    dev.api_client = AsyncMock()
    dev.api_client.async_get_hourly.return_value = [
        {"start": datetime(2026, 11, 24, 0, tzinfo=KST), "usage": 1.0, "export": 0.0}
    ]
    return dev


async def test_device_sync_backfills_and_throttles(device):
    with patch(
        "custom_components.korea_incubator.kepco.device.async_import_hourly",
        AsyncMock(),
    ) as imp:
        await device.async_sync_hourly_statistics()
        assert device.api_client.async_get_hourly.await_count == 7
        await device.async_sync_hourly_statistics()  # 30분 내 재호출은 생략
        assert device.api_client.async_get_hourly.await_count == 7
        await device.async_sync_hourly_statistics(force=True)
        assert device.api_client.async_get_hourly.await_count == 10
    assert imp.await_count == 2
    assert imp.await_args.args[1] == "kepco_test_user"


async def test_device_sync_failure_does_not_break_update(device):
    device.api_client.async_get_hourly.side_effect = RuntimeError("boom")
    device.api_client.async_get_recent_usage.return_value = {}
    device.api_client.async_get_usage_info.return_value = {}
    await device.async_update()
    assert device.available is True
