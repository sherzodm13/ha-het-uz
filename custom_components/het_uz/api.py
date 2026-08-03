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

# ponytail: refresh slightly before expiry; upgrade path is configurable buffer only
TOKEN_REFRESH_BUFFER = 60


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


def _is_auth_failure(status: int, payload: dict[str, Any]) -> bool:
    """Detect expired or rejected tokens even when HTTP status stays 200."""
    if status in (401, 403):
        return True
    api_status = payload.get("status")
    if isinstance(api_status, str):
        upper = api_status.upper()
        if upper in ("UNAUTHORIZED", "FORBIDDEN", "TOKEN_EXPIRED"):
            return True
    message = payload.get("message")
    if isinstance(message, str):
        lower = message.lower()
        if any(
            phrase in lower
            for phrase in ("token", "авториз", "authorization", "unauthorized")
        ):
            return True
    return False


# ponytail: fail fast if token helper logic regresses
assert _parse_api_timestamp("2026-08-03T11:59:13.904696391") is not None
assert _parse_api_timestamp("not-a-date") is None
assert _response_ok({"message": "Successfully loaded", "data": {}})
assert not _response_ok({"status": "BAD_REQUEST", "message": "nope"})
assert _is_auth_failure(401, {})
assert not _is_auth_failure(200, {"status": "BAD_REQUEST"})


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

    async def _json(self, response: ClientResponse) -> dict[str, Any]:
        try:
            payload = await response.json(content_type=None)
        except (ValueError, ClientError) as err:
            raise HetApiConnectionError("HET returned an invalid response") from err
        if not isinstance(payload, dict):
            raise HetApiConnectionError("HET returned an unexpected response")
        return payload

    async def async_login(self) -> None:
        """Authenticate and keep the bearer token in memory only."""
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
            raise HetApiConnectionError("Cannot connect to HET") from err

        if status in (401, 403):
            raise HetApiAuthError("Invalid login or password")
        if status >= 400:
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

    async def async_get_state(self) -> HetState:
        """Fetch cabinet values, retrying once with a fresh token."""
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

            if _is_auth_failure(status, payload):
                self._token = None
                self._token_expires_at = None
                if attempt == 0:
                    await self.async_login()
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
