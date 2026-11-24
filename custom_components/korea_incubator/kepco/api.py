import json
from datetime import date, datetime, timedelta, timezone

from bs4 import BeautifulSoup
from curl_cffi import AsyncSession, CurlError

from ..const import LOGGER
from ..utils import RSAKey
from .exceptions import KepcoAuthError

KST = timezone(timedelta(hours=9))


def _to_float(value) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def parse_hourly_details(day: date, payload) -> list[dict]:
    """real_day/usage.do 응답을 시간별 목록으로 변환한다.

    MR_HH=h(1~24)는 (h-1)시~h시 구간이므로 start는 해당 구간의 시작 시각(KST)이다.
    한전은 아직 집계되지 않은 시간도 0.000으로 내려주므로, 사용량/잉여량이
    모두 0이 아닌 마지막 시간까지만 반환한다(그 뒤는 미집계로 간주).
    """
    details = (payload or {}).get("details") or []
    midnight = datetime(day.year, day.month, day.day, tzinfo=KST)
    points = []
    for item in details:
        hour_raw = item.get("MR_HH") or str(item.get("MR_HHMI", ""))[:2]
        try:
            hour = int(hour_raw)
        except (TypeError, ValueError):
            continue
        if not 1 <= hour <= 24:
            continue
        points.append(
            {
                "start": midnight + timedelta(hours=hour - 1),
                "usage": _to_float(item.get("F_AP_QT")),
                "export": _to_float(item.get("F_AP_QT_REV")),
            }
        )
    points.sort(key=lambda point: point["start"])
    last = -1
    for index, point in enumerate(points):
        if point["usage"] > 0 or point["export"] > 0:
            last = index
    return points[: last + 1]


class KepcoApiClient:
    def __init__(self, session: AsyncSession):
        self._session = session
        self._username = None
        self._password = None
        self.last_error: str | None = None

    def set_credentials(self, username, password):
        self._username = username
        self._password = password

    async def async_get_session_and_rsa_key(self):
        url = "https://pp.kepco.co.kr:8030/intro.do"

        result = await self._session.get(url=url)
        result.raise_for_status()
        LOGGER.debug(f"Intro page response status: {result.status_code}")
        html_text = result.text

        soup = BeautifulSoup(html_text, "html.parser")

        rsa_modulus_tag = soup.find("input", {"id": "RSAModulus"})
        rsa_exponent_tag = soup.find("input", {"id": "RSAExponent"})
        sessid_tag = soup.find("input", {"id": "SESSID"})

        if not rsa_modulus_tag or not rsa_exponent_tag or not sessid_tag:
            raise KepcoAuthError(
                "Failed to get RSA modulus, exponent or SESSID from intro page HTML."
            )

        rsa_modulus = (rsa_modulus_tag.get("value") or "").strip()
        rsa_exponent = (rsa_exponent_tag.get("value") or "").strip()
        sessid = (sessid_tag.get("value") or "").strip()
        if not all((rsa_modulus, rsa_exponent, sessid)):
            raise KepcoAuthError("Empty RSA parameters or SESSID")

        return rsa_modulus, rsa_exponent, sessid

    async def async_login(self, username, password):
        self.set_credentials(username, password)
        self.last_error = None
        if not username or not password:
            self.last_error = "Username and password are required"
            return False
        try:
            (
                rsa_modulus,
                rsa_exponent,
                sessid,
            ) = await self.async_get_session_and_rsa_key()
        except KepcoAuthError as e:
            LOGGER.error(f"KEPCO Login failed: {e}")
            self.last_error = str(e)
            return False
        except (CurlError, OSError) as err:
            self.last_error = f"KEPCO session request failed ({type(err).__name__})"
            LOGGER.warning(self.last_error)
            return False

        try:
            rsa_key = RSAKey()
            rsa_key.set_public(rsa_modulus, rsa_exponent)

            encrypted_username_hex = rsa_key.encrypt(username)
            encrypted_password_hex = rsa_key.encrypt(password)

            if not encrypted_username_hex or not encrypted_password_hex:
                raise ValueError("RSA encryption failed")

        except (ValueError, TypeError, OverflowError) as e:
            LOGGER.error(f"RSA encryption failed: {e}")
            self.last_error = f"RSA encryption failed: {e}"
            return False

        user_id = f"{sessid}_{encrypted_username_hex}"
        user_pw = f"{sessid}_{encrypted_password_hex}"

        login_url = "https://pp.kepco.co.kr:8030/login"

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://pp.kepco.co.kr:8030/intro.do",
            "Cookie": f"JSESSIONID={sessid}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        }

        try:
            response = await self._session.post(
                login_url,
                data={"USER_ID": user_id, "USER_PW": user_pw},
                headers=headers,
                allow_redirects=True,
            )
            LOGGER.debug(f"Login response status: {response.status_code}")
            if response.status_code == 200 and "confirmInfo.do" in str(response.url):
                return True
            self.last_error = f"HTTP {response.status_code}: login rejected"
            LOGGER.warning("KEPCO %s", self.last_error)
            return False
        except (CurlError, OSError) as e:
            self.last_error = f"Login request failed ({type(e).__name__})"
            LOGGER.warning("KEPCO %s", self.last_error)
            return False

    async def _request(self, method, url, **kwargs):
        for attempt in range(2):
            response = await self._session.request(method, url, **kwargs)
            redirected_to_login = (
                str(response.url).split("?", 1)[0].endswith(("/login", "/intro.do"))
            )
            if response.status_code in (401, 403) or redirected_to_login:
                if attempt == 0 and await self.async_login(
                    self._username, self._password
                ):
                    continue
                raise KepcoAuthError("KEPCO session expired; authentication failed")
            response.raise_for_status()
            return json.loads(response.text)

    async def async_get_recent_usage(self):
        url = "https://pp.kepco.co.kr:8030/low/main/recent_usage.do"
        return await self._request("POST", url, json={})

    async def async_get_usage_info(self):
        url = "https://pp.kepco.co.kr:8030/low/main/usage_info.do"
        return await self._request("POST", url, json={"tou": "N"})

    async def async_get_hourly(self, day: date) -> list[dict]:
        """하루치 시간별 사용량(수전)과 잉여량(역송)을 함께 조회한다."""
        url = "https://pp.kepco.co.kr:8030/low/real/real_day/usage.do"
        payload = await self._request(
            "POST", url, json={"SEARCH_DT": day.strftime("%Y-%m-%d")}
        )
        return parse_hourly_details(day, payload)

    async def async_get_hourly_usage(self, day: date) -> list[tuple[datetime, float]]:
        """시간별 사용량(kWh): [(구간 시작 시각, 값)]"""
        return [(p["start"], p["usage"]) for p in await self.async_get_hourly(day)]

    async def async_get_hourly_export(self, day: date) -> list[tuple[datetime, float]]:
        """시간별 잉여량(태양광 역송, kWh): [(구간 시작 시각, 값)]"""
        return [(p["start"], p["export"]) for p in await self.async_get_hourly(day)]
