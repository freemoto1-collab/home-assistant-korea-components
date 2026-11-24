import asyncio
import time
from datetime import datetime, timedelta

import aiohttp
from curl_cffi import AsyncSession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import UpdateFailed

from .api import KST, KepcoApiClient, mask_custno
from .exceptions import KepcoAuthError
from ..const import DOMAIN, LOGGER
from .statistics import async_import_hourly, summarize_hourly

HOURLY_SYNC_INTERVAL = 1800  # 초
HOURLY_DAYS_INITIAL = 14
HOURLY_DAYS_REFRESH = 3


_ACCOUNT_LOCKS: dict[str, asyncio.Lock] = {}


def _account_lock(username: str) -> asyncio.Lock:
    """같은 아이디의 항목들은 서버 세션 상태(고객번호 선택)를 공유할 수 있어 직렬화한다."""
    lock = _ACCOUNT_LOCKS.get(username)
    if lock is None:
        lock = _ACCOUNT_LOCKS[username] = asyncio.Lock()
    return lock


def kepco_unique_id(username: str, custno: str | None = None) -> str:
    """기존 항목은 kepco_<아이디> 그대로, 로그인 아이디와 다른 고객번호만 접미사."""
    if custno and str(custno) != str(username):
        return f"kepco_{username}_{custno}"
    return f"kepco_{username}"


def kepco_display_name(
    username: str, custno: str | None = None, multiple: bool = False
) -> str:
    """표시 이름: 고객번호가 여러 개일 때만 뒤 4자리로 구분한다."""
    if multiple:
        return f"한전 ({mask_custno(custno or username)})"
    return "한전"


class KepcoDevice:
    def __init__(
        self,
        hass,
        entry_id: str,
        username: str,
        password: str,
        session: aiohttp.ClientSession | AsyncSession,
        custno: str | None = None,
        has_export: bool = False,
    ):
        self.hass = hass
        self.entry_id = entry_id
        self.username = username
        self.password = password
        self.session = session
        self.api_client = KepcoApiClient(self.session)
        self.api_client.set_credentials(
            username, password
        )  # Set credentials for re-auth

        self.data = {}
        # 잉여(역송) 설비가 있는 고객번호인지. 한 번 True가 되면 유지(저장은 호출 측).
        self.has_export = bool(has_export)
        self.on_export_detected = None
        self.custno = str(custno) if custno else None
        self.api_client.set_customer(self.custno)
        self._customer_count: int | None = None
        # 고유 ID/통계 ID는 기존 값을 유지한다(기본 고객번호는 접미사 없음).
        self._unique_id = kepco_unique_id(username, self.custno)
        self._name = self.display_name
        self._available = True
        self.data = {}  # 초기화 추가
        self._last_hourly_sync: float | None = None
        self._customer_checked: float | None = None

    @property
    def display_name(self) -> str:
        multiple = self.custno is not None or bool(
            self._customer_count and self._customer_count > 1
        )
        # 고객번호가 선택 저장된 항목은 이미 다중 고객 계정이다.
        current = self.custno or (self.data.get("usage_info") or {}).get("SESS_CUSTNO")
        return kepco_display_name(self.username, current, multiple)

    @property
    def unique_id(self) -> str:
        return self._unique_id

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._unique_id)},
            name=self.display_name,
            manufacturer="한국전력공사",
            model="KEPCO",
            configuration_url="https://pp.kepco.co.kr",
        )

    @property
    def available(self) -> bool:
        return self._available

    async def async_update(self):
        """Fetch data from KEPCO API."""
        try:
            async with _account_lock(self.username):
                await self._async_refresh_customer_count()
                recent_usage = await self.api_client.async_get_recent_usage()
                usage_info = await self.api_client.async_get_usage_info()
                self.data = {
                    "recent_usage": recent_usage,
                    "usage_info": usage_info,
                }
                self._available = True
                self._last_update_success = datetime.now()
                self._mark_export(usage_info=usage_info)
                await self.async_sync_hourly_statistics()
            LOGGER.debug(f"KEPCO data updated successfully for {self.username}")
        except KepcoAuthError as err:
            self._available = False
            LOGGER.error(
                f"Authentication error updating KEPCO data for {self.username}: {err}"
            )
            raise UpdateFailed(f"Authentication error: {err}")
        except Exception as err:
            self._available = False
            LOGGER.error(f"Error updating KEPCO data for {self.username}: {err}")
            raise UpdateFailed(f"Error communicating with KEPCO API: {err}")

    def _mark_export(self, usage_info=None, points=None) -> None:
        """잉여 보유 판단: 발전설비 구분(SESS_GENT_INST_CLCD)이 있거나 잉여량>0 이력."""
        if self.has_export:
            return
        found = bool(isinstance(usage_info, dict) and usage_info.get("SESS_GENT_INST_CLCD"))
        found = found or any((p.get("export") or 0) > 0 for p in points or [])
        if not found:
            return
        self.has_export = True
        if callable(self.on_export_detected):
            self.on_export_detected()

    async def _async_refresh_customer_count(self) -> None:
        """계정의 고객번호 수를 한 번(이후 1시간마다) 확인해 표시 이름에 반영한다."""
        now = time.monotonic()
        if (
            self._customer_count is not None
            and self._customer_checked is not None
            and now - self._customer_checked < 3600
        ):
            return
        try:
            customers = await self.api_client.async_get_customers()
            if isinstance(customers, list):
                self._customer_count = len(customers)
                self._customer_checked = now
                # 고객번호를 따로 저장하지 않은 항목(기존 항목)은 로그인 아이디와 같은
                # 고객번호를 자기 번호로 고정해, 다른 항목의 전환에 휩쓸리지 않게 한다.
                if (
                    self.custno is None
                    and len(customers) > 1
                    and any(c.get("custno") == self.username for c in customers)
                ):
                    self.api_client.set_customer(self.username)
        except KepcoAuthError:
            raise
        except Exception as err:
            LOGGER.debug(f"KEPCO customer list check failed: {err}")

    async def async_sync_hourly_statistics(self, force: bool = False) -> None:
        """시간별 사용량/잉여량을 해당 시각 통계로 기록(최근 며칠 재조회로 백필).

        센서용 최근 시간 값은 매 폴링마다 오늘 데이터로 갱신한다.
        실패해도 기존 센서 갱신에는 영향을 주지 않는다.
        """
        now = time.monotonic()
        due = (
            force
            or self._last_hourly_sync is None
            or now - self._last_hourly_sync >= HOURLY_SYNC_INTERVAL
        )
        try:
            today = datetime.now(KST).date()
            if due:
                days = (
                    HOURLY_DAYS_REFRESH
                    if self._last_hourly_sync
                    else HOURLY_DAYS_INITIAL
                )
                points = []
                for offset in range(days - 1, -1, -1):
                    points.extend(
                        await self.api_client.async_get_hourly(
                            today - timedelta(days=offset)
                        )
                    )
                today_points = [p for p in points if p["start"].date() == today]
                self._mark_export(points=points)
                self.data["hourly"] = summarize_hourly(today_points)
                await async_import_hourly(
                    self.hass,
                    self._unique_id,
                    self.display_name,
                    points,
                    kinds=("usage", "export") if self.has_export else ("usage",),
                )
                self._last_hourly_sync = now
            else:
                today_points = await self.api_client.async_get_hourly(today)
                self._mark_export(points=today_points)
            self.data["hourly"] = summarize_hourly(today_points)
        except KepcoAuthError:
            raise
        except Exception as err:
            LOGGER.warning(f"KEPCO hourly statistics sync failed: {err}")

    def get_current_usage(self):
        """현재 사용량 조회"""
        try:
            return self.data.get("recent_usage", {}).get("result", {}).get("F_AP_QT")
        except (KeyError, AttributeError):
            return None

    def get_last_month_bill(self):
        """지난달 요금 조회"""
        try:
            return (
                self.data.get("usage_info", {}).get("result", {}).get("BILL_LAST_MONTH")
            )
        except (KeyError, AttributeError):
            return None

    def get_predicted_bill(self):
        """예상 요금 조회"""
        try:
            return (
                self.data.get("usage_info", {})
                .get("result", {})
                .get("PREDICT_TOTAL_CHARGE_REV")
            )
        except (KeyError, AttributeError):
            return None

    async def async_close_session(self):
        """Close the aiohttp session."""
        if (
            self.session
            and isinstance(self.session, aiohttp.ClientSession)
            and not self.session.closed
        ):
            await self.session.close()
            self.session = None
        elif isinstance(self.session, AsyncSession):
            await self.session.close()
            self.session = None
