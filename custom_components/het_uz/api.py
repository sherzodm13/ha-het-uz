"""Async client for the HET Uzbekistan household cabinet API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import time
from typing import Any

from aiohttp import ClientError, ClientResponse, ClientSession

from .const import API_BASE_URL, LOGIN_PATH, REQUEST_TIMEOUT, STATE_PATH

# refresh slightly before expiry; upgrade path is configurable buffer only
TOKEN_REFRESH_BUFFER = 60
# module-level throttle survives HA setup retries; upgrade path is hass storage
LOGIN_MIN_INTERVAL = 30
LOGIN_RATE_LIMIT_BACKOFF = 3600
_login_throttle: dict[str, tuple[float, float]] = {}


class HetApiError(Exception):
    """Base HET API error."""


class HetApiAuthError(HetApiError):
    """Authentication failed."""


class HetApiConnectionError(HetApiError):
    """Communication with HET failed."""


@dataclass(frozen=True)
class HetState:
    """Values displayed on the household cabinet home page."""

    balance: Decimal | None
    current_month_kwh: Decimal | None
    current_month_amount: Decimal | None


def _decimal(value: Any) -> Decimal | None:
    """Convert a JSON number or numeric string without losing precision."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _parse_api_timestamp(value: Any) -> float | None:
    """Parse HET ISO timestamps like 2026-08-03T11:59:13.904696391."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _payload_field(payload: dict[str, Any], key: str) -> Any:
    """Read a field from the top level or nested data object."""
    value = payload.get(key)
    data = payload.get("data")
    if value is None and isinstance(data, dict):
        value = data.get(key)
    return value


def _response_ok(payload: dict[str, Any]) -> bool:
    """HET marks successful calls with OK/Successfully in status or message."""
    message = payload.get("message")
    if isinstance(message, str) and "successfully" in message.lower():
        return True
    status = payload.get("status")
    if isinstance(status, str) and status.upper() in ("OK", "SUCCESS", "SUCCESSFULLY"):
        return True
    return isinstance(payload.get("data"), dict)


def _rate_limit_message(payload: dict[str, Any]) -> str | None:
    """Return a rate-limit message when HET blocks further login attempts."""
    message = payload.get("message")
    if not isinstance(message, str):
        return None
    lower = message.lower()
    if any(
        phrase in lower
        for phrase in (
            "попыток закончилось",
            "try again later",
            "too many",
            "rate limit",
        )
    ):
        return message
    return None


def _is_rate_limited(status: int, payload: dict[str, Any]) -> bool:
    return status == 429 or _rate_limit_message(payload) is not None


def _is_auth_failure(status: int, payload: dict[str, Any]) -> bool:
    """Detect expired or invalid credentials; rate limits are handled separately."""
    if _is_rate_limited(status, payload):
        return False
    if status == 401:
        return True
    if status == 403:
        message = payload.get("message")
        if isinstance(message, str):
            lower = message.lower()
            return any(
                phrase in lower
                for phrase in ("token", "unauthorized", "авториз")
            )
        return False
    api_status = payload.get("status")
    if isinstance(api_status, str):
        upper = api_status.upper()
        if upper in ("UNAUTHORIZED", "TOKEN_EXPIRED", "FORBIDDEN"):
            return True
    message = payload.get("message")
    if isinstance(message, str):
        lower = message.lower()
        if any(
            phrase in lower
            for phrase in ("unauthorized", "token expired", "авториз")
        ):
            return True
    return False


# fail fast if token helper logic regresses
assert _parse_api_timestamp("2026-08-03T11:59:13.904696391") is not None
assert _parse_api_timestamp("not-a-date") is None
assert _response_ok({"message": "Successfully loaded", "data": {}})
assert not _response_ok({"status": "BAD_REQUEST", "message": "nope"})
assert _is_auth_failure(401, {})
assert not _is_auth_failure(200, {"status": "BAD_REQUEST"})
assert not _is_auth_failure(
    403, {"message": "Количество попыток закончилось, попробуйте позже!"}
)
assert _is_rate_limited(
    400, {"message": "Количество попыток закончилось, попробуйте позже!"}
)
assert _is_auth_failure(200, {"status": "FORBIDDEN"})
assert _is_auth_failure(200, {"message": "Token expired"})


class HetApiClient:
    """Client which caches a token and re-logs in before or after expiry."""

    def __init__(self, session: ClientSession, login: str, password: str) -> None:
        self._session = session
        self._login = login
        self._password = password
        self._token: str | None = None
        self._coato_code: str | None = None
        self._token_expires_at: float | None = None

    def _token_needs_refresh(self) -> bool:
        if self._token is None:
            return True
        if self._token_expires_at is None:
            return False
        return time.time() >= self._token_expires_at - TOKEN_REFRESH_BUFFER

    def _has_valid_token(self) -> bool:
        return self._token is not None and not self._token_needs_refresh()

    def _clear_token(self) -> None:
        self._token = None
        self._coato_code = None
        self._token_expires_at = None

    def _record_login_attempt(self) -> None:
        _login_throttle[self._login] = (time.time(), self._login_blocked_until())

    def _reset_login_interval(self) -> None:
        _login_throttle.pop(self._login, None)

    def _login_blocked_until(self) -> float:
        return _login_throttle.get(self._login, (0.0, 0.0))[1]

    def _raise_if_login_blocked(self) -> None:
        blocked_until = self._login_blocked_until()
        if time.time() < blocked_until:
            raise HetApiConnectionError(
                "HET login temporarily blocked due to rate limiting, try again later"
            )

    def _mark_login_rate_limited(self, message: str) -> None:
        now = time.time()
        _login_throttle[self._login] = (now, now + LOGIN_RATE_LIMIT_BACKOFF)
        raise HetApiConnectionError(message)

    async def _json(self, response: ClientResponse) -> dict[str, Any]:
        try:
            payload = await response.json(content_type=None)
        except (ValueError, ClientError) as err:
            raise HetApiConnectionError("HET returned an invalid response") from err
        if not isinstance(payload, dict):
            raise HetApiConnectionError("HET returned an unexpected response")
        return payload

    async def async_login(self, *, force: bool = False) -> None:
        """Authenticate and keep the bearer token in memory only."""
        self._raise_if_login_blocked()
        now = time.time()
        last_login, _ = _login_throttle.get(self._login, (0.0, 0.0))
        if not force and now - last_login < LOGIN_MIN_INTERVAL:
            if self._has_valid_token():
                return
            raise HetApiConnectionError(
                "HET login throttled locally, try again shortly"
            )

        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with self._session.post(
                    f"{API_BASE_URL}{LOGIN_PATH}",
                    json={"login": self._login, "password": self._password},
                    headers={"Origin": "https://cabinet.het.uz"},
                ) as response:
                    status = response.status
                    payload = await self._json(response)
        except (TimeoutError, ClientError) as err:
            self._record_login_attempt()
            raise HetApiConnectionError("Cannot connect to HET") from err

        if _is_rate_limited(status, payload):
            message = _rate_limit_message(payload) or f"HTTP {status}"
            self._mark_login_rate_limited(str(message))
        if _is_auth_failure(status, payload):
            self._clear_token()
            self._reset_login_interval()
            raise HetApiAuthError("Invalid login or password")
        if status >= 400:
            self._record_login_attempt()
            message = payload.get("message") or f"HTTP {status}"
            raise HetApiConnectionError(str(message))

        token = _payload_field(payload, "accessToken")
        if not isinstance(token, str) or not token:
            message = payload.get("message") or "HET did not return an access token"
            raise HetApiAuthError(str(message))
        coato_code = _payload_field(payload, "coatoCode")
        if coato_code is None:
            raise HetApiAuthError("HET did not return a Coato-Code")

        expires_in = _payload_field(payload, "expiresIn")
        issued_at = _parse_api_timestamp(payload.get("timestamp")) or time.time()
        if isinstance(expires_in, (int, float)) and expires_in > 0:
            self._token_expires_at = issued_at + float(expires_in)
        else:
            self._token_expires_at = None

        self._token = token
        self._coato_code = str(coato_code)
        _login_throttle[self._login] = (time.time(), 0.0)

    async def async_get_state(self) -> HetState:
        """Fetch cabinet values, retrying once with a fresh token."""
        self._raise_if_login_blocked()
        if self._token_needs_refresh():
            await self.async_login()

        for attempt in range(2):
            try:
                async with asyncio.timeout(REQUEST_TIMEOUT):
                    async with self._session.get(
                        f"{API_BASE_URL}{STATE_PATH}",
                        headers={
                            "Authorization": f"Bearer {self._token}",
                            "Coato-Code": self._coato_code or "",
                            "lang": "RU",
                            "Origin": "https://cabinet.het.uz",
                        },
                    ) as response:
                        status = response.status
                        payload = await self._json(response)
            except (TimeoutError, ClientError) as err:
                raise HetApiConnectionError("Cannot connect to HET") from err

            if _is_rate_limited(status, payload):
                message = _rate_limit_message(payload) or f"HTTP {status}"
                self._mark_login_rate_limited(str(message))
            if _is_auth_failure(status, payload):
                self._clear_token()
                if attempt == 0:
                    await self.async_login(force=True)
                    continue
                raise HetApiAuthError("HET rejected the credentials")
            if status >= 400:
                message = payload.get("message") or f"HTTP {status}"
                raise HetApiConnectionError(str(message))
            if not _response_ok(payload):
                message = payload.get("message") or "HET returned an unsuccessful response"
                raise HetApiConnectionError(str(message))

            data = payload.get("data")
            if not isinstance(data, dict):
                raise HetApiConnectionError("HET response has no data object")
            return HetState(
                balance=(
                    value / Decimal(100)
                    if (value := _decimal(data.get("balance"))) is not None
                    else None
                ),
                current_month_kwh=(
                    value / Decimal(1000)
                    if (value := _decimal(data.get("currentMonthCalcKwh"))) is not None
                    else None
                ),
                current_month_amount=(
                    value / Decimal(100)
                    if (value := _decimal(data.get("currentMonthCalcSum"))) is not None
                    else None
                ),
            )

        raise HetApiAuthError("HET authentication failed")
