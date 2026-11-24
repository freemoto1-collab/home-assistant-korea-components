"""잉여(역송)가 없는 고객번호는 잉여량 센서/통계를 만들지 않는다 (합성 번호)."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from curl_cffi import AsyncSession

from custom_components.korea_incubator import async_setup_entry as init_setup
from custom_components.korea_incubator.const import DOMAIN
from custom_components.korea_incubator.kepco import statistics as stats
from custom_components.korea_incubator.kepco.api import KST
from custom_components.korea_incubator.kepco.device import KepcoDevice
from custom_components.korea_incubator.sensor import async_setup_entry

EXPORT_NAMES = {"최근 집계 시간 잉여량", "오늘 잉여량 누계"}


def make_device(has_export=False):
    session = MagicMock(spec=AsyncSession)
    session.close = AsyncMock()
    return KepcoDevice(
        MagicMock(), "e", "1111114372", "pw", session, has_export=has_export
    )


async def run_sensor_setup(device):
    hass = MagicMock()
    hass.data = {DOMAIN: {"entry1": {"coordinator": MagicMock(data={}), "device": device}}}
    entry = MagicMock(entry_id="entry1", data={"service": "kepco"})
    added = []
    registry = MagicMock()
    registry.async_get_entity_id.side_effect = lambda domain, platform, uid: f"sensor.{uid}"
    with patch("custom_components.korea_incubator.sensor.er.async_get", return_value=registry):
        await async_setup_entry(hass, entry, lambda ents, *a, **k: added.extend(ents))
    return {e._attr_name for e in added}, registry


async def test_no_export_means_no_export_sensors_and_cleanup():
    names, registry = await run_sensor_setup(make_device(has_export=False))
    assert not names & EXPORT_NAMES
    assert "최근 집계 시간 사용량" in names
    removed = {c.args[0] for c in registry.async_remove.call_args_list}
    assert len(removed) == 2 and all(r.endswith(("hourly_export", "hourly_today_export")) for r in removed)


async def test_export_customer_keeps_export_sensors():
    names, registry = await run_sensor_setup(make_device(has_export=True))
    assert EXPORT_NAMES <= names
    registry.async_remove.assert_not_called()


def test_detect_by_generation_field_and_stays_true():
    dev = make_device()
    dev._mark_export(usage_info={"SESS_GENT_INST_CLCD": None})
    assert dev.has_export is False
    dev._mark_export(usage_info={"SESS_GENT_INST_CLCD": "01"})
    assert dev.has_export is True
    # 잉여가 0인 날에도 유지
    dev._mark_export(points=[{"export": 0.0}])
    assert dev.has_export is True


def test_detect_by_positive_export_hour_triggers_callback_once():
    dev = make_device()
    cb = MagicMock()
    dev.on_export_detected = cb
    dev._mark_export(points=[{"export": 0.0}, {"export": 0.0}])
    assert dev.has_export is False and not cb.called
    dev._mark_export(points=[{"export": 0.0}, {"export": 0.1}])
    dev._mark_export(points=[{"export": 0.2}])
    assert dev.has_export is True
    cb.assert_called_once()


async def test_stats_skip_export_when_no_export():
    t0 = datetime(2026, 11, 24, 0, tzinfo=KST)
    points = [{"start": t0, "usage": 1.0, "export": 0.0}]
    calls = []
    with patch.object(stats, "_base_sum", AsyncMock(return_value=0.0)), patch(
        "homeassistant.components.recorder.statistics.async_add_external_statistics",
        side_effect=lambda hass, meta, data: calls.append(meta["statistic_id"]),
    ):
        await stats.async_import_hourly(MagicMock(), "kepco_x", "한전", points, kinds=("usage",))
    assert calls == ["korea_incubator:kepco_x_hourly_usage"]


async def test_setup_persists_flag_and_reloads_on_first_export(kepco_config_data):
    hass = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.data = {DOMAIN: {}}
    entry = MagicMock(entry_id="e1", data=dict(kepco_config_data), title="한전")
    coordinator = MagicMock()
    coordinator.async_config_entry_first_refresh = AsyncMock()
    with patch("custom_components.korea_incubator.KepcoDevice") as cls, patch(
        "custom_components.korea_incubator.curl_cffi.AsyncSession"
    ), patch(
        "custom_components.korea_incubator.DataUpdateCoordinator",
        return_value=coordinator,
    ):
        device = AsyncMock()
        device.has_export = False
        device.display_name = "한전"
        device.api_client = MagicMock(async_login=AsyncMock())
        cls.return_value = device
        await init_setup(hass, entry)
        assert device.on_export_detected is not None
        hass.config_entries.async_update_entry.reset_mock()
        device.on_export_detected()
    args = hass.config_entries.async_update_entry.call_args
    assert args.kwargs["data"]["has_export"] is True
    hass.config_entries.async_schedule_reload.assert_called_once_with("e1")
