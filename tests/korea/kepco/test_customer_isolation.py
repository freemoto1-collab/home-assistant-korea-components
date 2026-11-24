"""같은 계정의 두 고객번호 항목이 서로의 데이터를 읽지 않는지 검증 (합성 번호)."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.korea_incubator.kepco.api import KepcoApiClient
from custom_components.korea_incubator.kepco.device import KepcoDevice
from custom_components.korea_incubator.kepco.exceptions import (
    KepcoCustomerMismatchError,
)
from custom_components.korea_incubator.sensor import KepcoHourlySensor, KoreaSensor
from custom_components.korea_incubator.kepco.api import mask_custno

A = "1111114372"  # 로그인 아이디와 같은 기본 고객번호, 잉여량 있음
B = "2222224381"  # 두 번째 고객번호, 잉여량 없음

CHOOSE_HTML = (
    f'chooseArray.push({{ CUSTNO : "{A}", METER_ID : "9001" }});'
    f'chooseArray.push({{ CUSTNO : "{B}", METER_ID : "9002" }});'
)

DATA = {
    A: {"usage": "123.20", "export": 0.5},
    B: {"usage": "10.00", "export": 0.0},
}


class FakeServer:
    """고객번호 선택이 (아이디 단위로) 모든 세션에서 공유되는 최악의 서버."""

    def __init__(self, ignore_switch=False):
        self.selected = A
        self.ignore_switch = ignore_switch
        self.switches = []

    def session(self):
        server = self

        class Session:
            async def get(self, url, **kw):
                return MagicMock(status_code=200, text=CHOOSE_HTML, url=url)

            async def post(self, url, **kw):
                if url.endswith("customchoosechange.do"):
                    server.switches.append(kw["json"]["custno"])
                    if not server.ignore_switch:
                        server.selected = kw["json"]["custno"]
                    return MagicMock(status_code=200, text='"low"', url=url)
                return await self.request("POST", url, **kw)

            async def request(self, method, url, **kw):
                cust = server.selected
                if url.endswith("usage_info.do"):
                    body = {
                        "SESS_CUSTNO": cust,
                        "result": {"F_AP_QT": DATA[cust]["usage"]},
                    }
                elif url.endswith("recent_usage.do"):
                    body = {"SESS_CUSTNO": cust, "result": {"F_AP_QT": "1"}}
                else:  # real_day/usage.do
                    body = {
                        "SESS_CUSTNO": cust,
                        "details": [
                            {
                                "MR_HH": "01",
                                "F_AP_QT": "0.4",
                                "F_AP_QT_REV": str(DATA[cust]["export"]),
                            }
                        ],
                    }
                return MagicMock(status_code=200, text=json.dumps(body), url=url)

        return Session()


def make_device(server, custno=None):
    dev = KepcoDevice(MagicMock(), "e", A, "pw", server.session(), custno=custno)
    dev.api_client.async_login = AsyncMock(return_value=True)
    return dev


async def test_mismatch_triggers_reselect_and_returns_own_data():
    server = FakeServer()
    client = KepcoApiClient(server.session())
    client.set_customer(B)  # 서버는 A를 선택한 상태
    info = await client.async_get_usage_info()
    assert info["SESS_CUSTNO"] == B
    assert server.switches == [B]


async def test_persistent_mismatch_is_rejected_not_returned():
    server = FakeServer(ignore_switch=True)  # 전환이 적용되지 않는 경우
    client = KepcoApiClient(server.session())
    client.set_customer(B)
    with pytest.raises(KepcoCustomerMismatchError) as err:
        await client.async_get_usage_info()
    assert server.switches == [B, B]  # 2회까지만 재시도
    assert A not in str(err.value) and B not in str(err.value)  # 번호 전체 미노출


async def test_two_devices_updating_concurrently_never_mix():
    server = FakeServer()
    dev_a = make_device(server)  # 번호를 저장하지 않은 기존 항목
    dev_b = make_device(server, custno=B)
    imports = {}

    kinds_used = {}

    async def fake_import(hass, uid, name, points, kinds=("usage", "export")):
        imports[uid] = points
        kinds_used[uid] = kinds

    with patch(
        "custom_components.korea_incubator.kepco.device.async_import_hourly",
        side_effect=fake_import,
    ):
        for _ in range(3):  # 서로 번갈아 선택을 덮어쓰는 상황을 반복
            await asyncio.gather(dev_a.async_update(), dev_b.async_update())
            dev_a._last_hourly_sync = None
            dev_b._last_hourly_sync = None

    assert dev_a.data["usage_info"]["SESS_CUSTNO"] == A
    assert dev_a.data["usage_info"]["result"]["F_AP_QT"] == "123.20"
    assert dev_b.data["usage_info"]["SESS_CUSTNO"] == B
    assert dev_b.data["usage_info"]["result"]["F_AP_QT"] == "10.00"
    # 기존(번호 미저장) 항목도 아이디와 같은 번호로 고정되어 B의 전환에 영향받지 않는다
    assert dev_a.api_client._custno == A
    # 시간별 통계: B는 잉여량 0, A는 0.5
    assert all(p["export"] == 0.0 for p in imports[dev_b.unique_id])
    assert any(p["export"] == 0.5 for p in imports[dev_a.unique_id])
    # 잉여 없는 B는 잉여량 통계를 쓰지 않고, 있는 A는 쓴다
    assert kinds_used[dev_b.unique_id] == ("usage",)
    assert kinds_used[dev_a.unique_id] == ("usage", "export")
    assert dev_b.data["hourly"]["export"] == 0.0
    assert dev_b.data["hourly"]["today_export"] == 0.0
    assert dev_a.data["hourly"]["today_export"] == 0.5


async def test_customer_sensor_shows_own_masked_number():
    server = FakeServer()
    dev_b = make_device(server, custno=B)
    await dev_b.async_update()
    coordinator = MagicMock(data=dev_b.data)
    sensor = KoreaSensor(
        coordinator, dev_b, "usage_info", "SESS_CUSTNO", "고객번호",
        None, None, None, value_transform=mask_custno,
    )
    assert sensor.native_value == "******4381"
    export = KepcoHourlySensor(coordinator, dev_b, "export", "최근 집계 시간 잉여량")
    assert export.native_value == 0.0


async def test_updates_of_same_account_are_serialized():
    server = FakeServer()
    dev_a, dev_b = make_device(server), make_device(server, custno=B)
    events = []

    def slow(dev, name):
        async def call():
            events.append(f"{name}-start")
            await asyncio.sleep(0.01)
            events.append(f"{name}-end")
            return {"SESS_CUSTNO": dev.api_client._custno or A}

        return call

    dev_a.api_client = MagicMock(
        async_get_customers=AsyncMock(return_value=[]),
        async_get_recent_usage=slow(dev_a, "a"),
        async_get_usage_info=AsyncMock(return_value={}),
        async_get_hourly=AsyncMock(return_value=[]),
    )
    dev_b.api_client = MagicMock(
        async_get_customers=AsyncMock(return_value=[]),
        async_get_recent_usage=slow(dev_b, "b"),
        async_get_usage_info=AsyncMock(return_value={}),
        async_get_hourly=AsyncMock(return_value=[]),
    )
    with patch(
        "custom_components.korea_incubator.kepco.device.async_import_hourly",
        AsyncMock(),
    ):
        await asyncio.gather(dev_a.async_update(), dev_b.async_update())
    assert events in (
        ["a-start", "a-end", "b-start", "b-end"],
        ["b-start", "b-end", "a-start", "a-end"],
    )


async def test_device_update_fails_instead_of_writing_wrong_data():
    from homeassistant.helpers.update_coordinator import UpdateFailed

    server = FakeServer(ignore_switch=True)
    dev_b = make_device(server, custno=B)
    with pytest.raises(UpdateFailed):
        await dev_b.async_update()
    assert dev_b.available is False
    assert "usage_info" not in dev_b.data
