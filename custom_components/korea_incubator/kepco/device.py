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
HOURLY_DAYS_INITIAL = 7
HOURLY_DAYS_REFRESH = 3


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
            recent_usage = await self.api_client.async_get_recent_usage()
            usage_info = await self.api_client.async_get_usage_info()
            self.data = {
                "recent_usage": recent_usage,
                "usage_info": usage_info,
            }
            self._available = True
            self._last_update_success = datetime.now()
            await self._async_refresh_customer_count()
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
                await async_import_hourly(
                    self.hass, self._unique_id, self.display_name, points
                )
                self._last_hourly_sync = now
            else:
                today_points = await self.api_client.async_get_hourly(today)
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
