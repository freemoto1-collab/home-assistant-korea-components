import time
from datetime import datetime, timedelta

import aiohttp
from curl_cffi import AsyncSession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import UpdateFailed

from .api import KST, KepcoApiClient
from .exceptions import KepcoAuthError
from ..const import DOMAIN, LOGGER
from .statistics import async_import_hourly

HOURLY_SYNC_INTERVAL = 1800  # 초
HOURLY_DAYS_INITIAL = 7
HOURLY_DAYS_REFRESH = 3


class KepcoDevice:
    def __init__(
        self,
        hass,
        entry_id: str,
        username: str,
        password: str,
        session: aiohttp.ClientSession | AsyncSession,
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

        self._name = f"한전 ({username})"
        self._unique_id = f"kepco_{username}"
        self._available = True
        self.data = {}  # 초기화 추가
        self._last_hourly_sync: float | None = None

    @property
    def unique_id(self) -> str:
        return self._unique_id

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._unique_id)},
            name=self._name,
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

    async def async_sync_hourly_statistics(self, force: bool = False) -> None:
        """시간별 사용량/잉여량을 해당 시각 통계로 기록(최근 며칠 재조회로 백필).

        실패해도 기존 센서 갱신에는 영향을 주지 않는다.
        """
        now = time.monotonic()
        if (
            not force
            and self._last_hourly_sync is not None
            and now - self._last_hourly_sync < HOURLY_SYNC_INTERVAL
        ):
            return
        days = HOURLY_DAYS_REFRESH if self._last_hourly_sync else HOURLY_DAYS_INITIAL
        try:
            today = datetime.now(KST).date()
            points = []
            for offset in range(days - 1, -1, -1):
                points.extend(
                    await self.api_client.async_get_hourly(
                        today - timedelta(days=offset)
                    )
                )
            await async_import_hourly(self.hass, self._unique_id, self._name, points)
            self._last_hourly_sync = now
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
