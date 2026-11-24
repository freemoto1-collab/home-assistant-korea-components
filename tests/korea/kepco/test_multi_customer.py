"""KEPCO 다중 고객번호/마스킹/시간별 센서 테스트 (합성 번호만 사용)."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from curl_cffi import AsyncSession

from custom_components.korea_incubator.config_flow import KoreaConfigFlow
from custom_components.korea_incubator.kepco.api import (
    KST,
    KepcoApiClient,
    mask_custno,
    parse_customers,
)
from custom_components.korea_incubator.kepco.device import (
    KepcoDevice,
    kepco_display_name,
    kepco_unique_id,
)
from custom_components.korea_incubator.kepco.statistics import (
    statistic_id,
    summarize_hourly,
)

HTML = """
<script>
chooseArray.push( { CUSTNO : "1111111111", METER_ID : "900001", REAL_METER_ID : "x", SESS_METER_YN : "Y", ALIAS : "" });
chooseArray.push( { CUSTNO : "1111111111", METER_ID : "900002", REAL_METER_ID : "x", SESS_METER_YN : "Y", ALIAS : "" });
chooseArray.push( { CUSTNO : "2222222222", METER_ID : "900003", REAL_METER_ID : "x", SESS_METER_YN : "Y", ALIAS : "" });
</script>
"""


def test_mask_custno():
    assert mask_custno("1111112222") == "******2222"
    assert mask_custno("123") == "123"
    assert mask_custno(None) == ""


def test_parse_customers_groups_meters():
    assert parse_customers(HTML) == [
        {"custno": "1111111111", "meterids": ["900001", "900002"]},
        {"custno": "2222222222", "meterids": ["900003"]},
    ]
    assert parse_customers("") == []


def test_unique_id_and_names_stay_compatible():
    assert kepco_unique_id("1111111111") == "kepco_1111111111"
    assert kepco_unique_id("1111111111", "1111111111") == "kepco_1111111111"
    assert kepco_unique_id("1111111111", "2222222222") == "kepco_1111111111_2222222222"
    assert kepco_display_name("1111111111") == "한전"
    assert kepco_display_name("1111111111", "2222222222", True) == "한전 (******2222)"
    # 통계 ID에는 번호 전체가 유지된다(이미 쌓인 데이터 보존)
    assert (
        statistic_id(kepco_unique_id("1111111111"), "usage")
        == "korea_incubator:kepco_1111111111_hourly_usage"
    )
    assert statistic_id(kepco_unique_id("1111111111", "2222222222"), "export").endswith(
        "kepco_1111111111_2222222222_hourly_export"
    )


def resp(text="", status=200, url="https://pp.kepco.co.kr:8030/x"):
    return MagicMock(status_code=status, text=text, url=url)


async def test_select_customer_posts_all_meters_of_that_customer():
    session = MagicMock(spec=AsyncSession)
    session.get = AsyncMock(return_value=resp(HTML))
    session.post = AsyncMock(return_value=resp('"low"'))
    client = KepcoApiClient(session)
    assert await client._select_customer("1111111111") is True
    assert session.post.call_args.args[0].endswith("/common/customchoosechange.do")
    assert session.post.call_args.kwargs["json"] == {
        "custno": "1111111111",
        "meterids": "900001,900002",
    }


async def test_select_unknown_customer_fails():
    session = MagicMock(spec=AsyncSession)
    session.get = AsyncMock(return_value=resp(HTML))
    session.post = AsyncMock()
    client = KepcoApiClient(session)
    assert await client._select_customer("9999999999") is False
    session.post.assert_not_awaited()


async def test_login_reselects_customer_each_time():
    """세션이 서버에 저장되므로 (재)로그인마다 고객번호를 다시 지정한다."""
    session = MagicMock(spec=AsyncSession)
    client = KepcoApiClient(session)
    client.set_customer("2222222222")
    with patch.object(
        client,
        "async_get_session_and_rsa_key",
        AsyncMock(return_value=("a", "10001", "sid")),
    ), patch("custom_components.korea_incubator.kepco.api.RSAKey") as rsa, patch.object(
        client, "_select_customer", AsyncMock(return_value=True)
    ) as select:
        rsa.return_value.encrypt.return_value = "ff"
        session.post = AsyncMock(
            return_value=resp(url="https://pp.kepco.co.kr:8030/confirmInfo.do")
        )
        assert await client.async_login("u", "p") is True
    select.assert_awaited_once_with("2222222222")


async def test_get_customers():
    session = MagicMock(spec=AsyncSession)
    session.get = AsyncMock(return_value=resp(HTML))
    client = KepcoApiClient(session)
    customers = await client.async_get_customers()
    assert [c["custno"] for c in customers] == ["1111111111", "2222222222"]


def make_device(custno=None):
    session = MagicMock(spec=AsyncSession)
    session.close = AsyncMock()
    return KepcoDevice(MagicMock(), "e", "1111111111", "pw", session, custno=custno)


def test_device_names_hide_number():
    single = make_device()
    assert single.display_name == "한전" and single.unique_id == "kepco_1111111111"
    single._customer_count = 2
    single.data = {"usage_info": {"SESS_CUSTNO": "1111111111"}}
    assert single.display_name == "한전 (******1111)"
    second = make_device("2222222222")
    assert second.unique_id == "kepco_1111111111_2222222222"
    assert second.display_name == "한전 (******2222)"
    assert "1111111111" not in str(second.device_info["name"])
    assert second.api_client._custno == "2222222222"


def test_summarize_hourly():
    t0 = datetime(2026, 11, 24, 8, tzinfo=KST)
    summary = summarize_hourly(
        [
            {"start": t0, "usage": 0.3, "export": 0.0},
            {"start": t0 + timedelta(hours=1), "usage": 0.2, "export": 0.25},
        ]
    )
    assert summary["usage"] == 0.2 and summary["export"] == 0.25
    assert summary["start"].startswith("2026-11-24T09:00")
    assert summary["end"].startswith("2026-11-24T10:00")
    assert summary["today_export"] == 0.25 and summary["today_usage"] == 0.5
    assert summarize_hourly([]) == {}


def test_sensor_exposes_hourly_values_and_interval_attributes():
    from custom_components.korea_incubator.sensor import KepcoHourlySensor

    coordinator = MagicMock()
    coordinator.data = {
        "hourly": {
            "usage": 0.176,
            "export": 0.246,
            "start": "2026-11-24T09:00:00+09:00",
            "end": "2026-11-24T10:00:00+09:00",
            "today_export": 0.251,
        }
    }
    sensor = KepcoHourlySensor(coordinator, make_device(), "export", "최근 집계 시간 잉여량")
    assert sensor.native_value == 0.246
    assert sensor.extra_state_attributes["집계 구간 시작"].startswith("2026-11-24T09")
    assert sensor.unique_id.endswith("hourly_export")


def test_customer_sensor_is_masked():
    from custom_components.korea_incubator.sensor import KoreaSensor

    sensor = KoreaSensor(
        MagicMock(data={"usage_info": {"SESS_CUSTNO": "1111112222"}}),
        make_device(),
        "usage_info",
        "SESS_CUSTNO",
        "고객번호",
        None,
        None,
        None,
        value_transform=mask_custno,
    )
    assert sensor.native_value == "******2222"


def fake_session():
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return patch(
        "custom_components.korea_incubator.config_flow.curl_cffi.AsyncSession",
        return_value=session,
    )


def make_flow():
    flow = KoreaConfigFlow()
    flow.hass = MagicMock()
    flow.hass.config_entries.async_entry_for_domain_unique_id.return_value = None
    flow.context = {}
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()
    flow._async_current_entries = MagicMock(return_value=[])
    flow.async_create_entry = MagicMock(
        side_effect=lambda **kw: {"type": "create_entry", **kw}
    )
    flow.async_show_form = MagicMock(side_effect=lambda **kw: {"type": "form", **kw})
    return flow


def client_with(customers):
    return MagicMock(
        async_login=AsyncMock(return_value=True),
        async_get_customers=AsyncMock(
            return_value=[{"custno": c, "meterids": ["1"]} for c in customers]
        ),
        set_credentials=MagicMock(),
        set_customer=MagicMock(),
    )


async def test_flow_single_customer_unchanged():
    flow = make_flow()
    with fake_session(), patch(
        "custom_components.korea_incubator.config_flow.KepcoApiClient",
        return_value=client_with(["1111111111"]),
    ):
        result = await flow.async_step_kepco(
            {"username": "1111111111", "password": "p"}
        )
    assert result["type"] == "create_entry"
    assert result["title"] == "한전"
    assert "custno" not in result["data"]
    flow.async_set_unique_id.assert_awaited_with("kepco_1111111111")


async def test_flow_multiple_customers_asks_and_creates_per_customer_entry():
    flow = make_flow()
    with fake_session(), patch(
        "custom_components.korea_incubator.config_flow.KepcoApiClient",
        return_value=client_with(["1111111111", "2222222222"]),
    ):
        form = await flow.async_step_kepco({"username": "1111111111", "password": "p"})
    assert form["type"] == "form" and form["step_id"] == "kepco_customer"
    labels = list(form["data_schema"].schema["custno"].container.values())
    assert labels == ["******1111", "******2222"]

    result = await flow.async_step_kepco_customer({"custno": "2222222222"})
    assert result["type"] == "create_entry"
    assert result["data"]["custno"] == "2222222222"
    assert result["title"] == "한전 (******2222)"
    flow.async_set_unique_id.assert_awaited_with("kepco_1111111111_2222222222")

    # 로그인 아이디와 같은 번호를 고르면 기존 항목과 같은 ID(중복 방지)
    result = await flow.async_step_kepco_customer({"custno": "1111111111"})
    flow.async_set_unique_id.assert_awaited_with("kepco_1111111111")
    assert result["data"]["custno"] == "1111111111"
