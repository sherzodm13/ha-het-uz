"""Async client for the HET Uzbekistan household cabinet API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from aiohttp import ClientError, ClientResponse, ClientSession

from .const import API_BASE_URL, LOGIN_PATH, REQUEST_TIMEOUT, STATE_PATH


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


class HetApiClient:
    """Client which caches a token and transparently logs in again on 401."""

    def __init__(self, session: ClientSession, login: str, password: str) -> None:
        self._session = session
        self._login = login
        self._password = password
        self._token: str | None = None

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

        data = payload.get("data")
        token = payload.get("accessToken")
        if token is None and isinstance(data, dict):
            token = data.get("accessToken")
        if not isinstance(token, str) or not token:
            message = payload.get("message") or "HET did not return an access token"
            raise HetApiAuthError(str(message))
        self._token = token

    async def async_get_state(self) -> HetState:
        """Fetch cabinet values, retrying once with a fresh token."""
        if self._token is None:
            await self.async_login()

        for attempt in range(2):
            try:
                async with asyncio.timeout(REQUEST_TIMEOUT):
                    async with self._session.get(
                        f"{API_BASE_URL}{STATE_PATH}",
                        headers={
                            "Authorization": f"Bearer {self._token}",
                            "Origin": "https://cabinet.het.uz",
                        },
                    ) as response:
                        status = response.status
                        payload = await self._json(response)
            except (TimeoutError, ClientError) as err:
                raise HetApiConnectionError("Cannot connect to HET") from err

            if status in (401, 403):
                self._token = None
                if attempt == 0:
                    await self.async_login()
                    continue
                raise HetApiAuthError("HET rejected the credentials")
            if status >= 400:
                message = payload.get("message") or f"HTTP {status}"
                raise HetApiConnectionError(str(message))

            data = payload.get("data")
            if not isinstance(data, dict):
                raise HetApiConnectionError("HET response has no data object")
            return HetState(
                balance=_decimal(data.get("balance")),
                current_month_kwh=_decimal(data.get("currentMonthCalcKwh")),
                current_month_amount=_decimal(data.get("currentMonthCalcSum")),
            )

        raise HetApiAuthError("HET authentication failed")
