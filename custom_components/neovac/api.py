"""NeoVac MyEnergy API client.

Handles authentication via auth.neovac.ch and data fetching via myenergy.neovac.ch.

The authentication flow works in two stages:
1. POST to auth.neovac.ch/api/v1/Account/Login to establish a session (cookie-based)
2. POST to myenergy.neovac.ch/api/v4/account/authenticate to get a Bearer token

The Bearer token is then used for all subsequent API calls to fetch consumption data.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

import aiohttp

from .const import (
    AUTH_IS_AUTHENTICATED_URL,
    AUTH_LOGIN_URL,
    MYENERGY_AUTH_URL,
    MYENERGY_API_URL,
    MYENERGY_USAGE_UNITS_URL,
    SUPPORTED_CATEGORIES,
)

_LOGGER = logging.getLogger(__name__)


class NeoVacAuthError(Exception):
    """Raised when authentication fails."""


class NeoVacApiError(Exception):
    """Raised when an API call fails."""


class NeoVacConnectionError(Exception):
    """Raised when the API is unreachable."""


class NeoVacApiClient:
    """Client to interact with the NeoVac MyEnergy API."""

    def __init__(
        self,
        email: str,
        password: str,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Initialize the API client.

        Args:
            email: NeoVac account email.
            password: NeoVac account password.
            session: Optional aiohttp session. If None, a new one is created.
        """
        self._email = email
        self._password = password
        self._session = session
        self._owns_session = session is None
        self._token: str | None = None
        self._cookie_jar: aiohttp.CookieJar | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Ensure we have an active aiohttp session."""
        if self._session is None or self._session.closed:
            self._cookie_jar = aiohttp.CookieJar()
            self._session = aiohttp.ClientSession(cookie_jar=self._cookie_jar)
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        """Close the session if we own it."""
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    @property
    def is_authenticated(self) -> bool:
        """Return whether we have a valid token."""
        return self._token is not None

    async def authenticate(self) -> bool:
        """Authenticate with NeoVac.

        Tries two approaches:
        1. Direct myenergy API authentication (POST /api/v4/account/authenticate)
        2. Two-step: auth.neovac.ch login first, then myenergy token exchange

        Returns True if authentication succeeded.
        Raises NeoVacAuthError on failure.
        """
        session = await self._ensure_session()

        # Strategy 1: Direct myenergy authentication
        try:
            token = await self._authenticate_direct(session)
            if token:
                self._token = token
                _LOGGER.debug("Authenticated via direct myenergy API")
                return True
        except Exception as err:
            _LOGGER.debug("Direct auth failed: %s, trying auth portal", err)

        # Strategy 2: Auth portal flow
        try:
            token = await self._authenticate_via_portal(session)
            if token:
                self._token = token
                _LOGGER.debug("Authenticated via auth portal")
                return True
        except Exception as err:
            _LOGGER.debug("Auth portal flow failed: %s", err)

        raise NeoVacAuthError("Authentication failed with all strategies")

    async def _authenticate_direct(
        self, session: aiohttp.ClientSession
    ) -> str | None:
        """Try direct authentication to myenergy API.

        POST /api/v4/account/authenticate with email and password.
        """
        payload = {
            "email": self._email,
            "password": self._password,
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            async with session.post(
                MYENERGY_AUTH_URL,
                json=payload,
                headers=headers,
            ) as resp:
                _LOGGER.debug(
                    "Direct auth response: status=%s", resp.status
                )
                if resp.status == 200:
                    data = await resp.json()
                    # The response should contain a session token
                    token = data.get("sessionToken") or data.get("token")
                    if token:
                        return token
                    # If the response is the token itself (string)
                    if isinstance(data, str):
                        return data
                    _LOGGER.debug("Direct auth response data: %s", data)
                    return None
                if resp.status in (401, 403):
                    text = await resp.text()
                    _LOGGER.debug("Direct auth rejected: %s", text)
                    return None
                text = await resp.text()
                _LOGGER.debug(
                    "Direct auth unexpected status %s: %s", resp.status, text
                )
                return None
        except aiohttp.ClientError as err:
            _LOGGER.debug("Direct auth connection error: %s", err)
            return None

    async def _authenticate_via_portal(
        self, session: aiohttp.ClientSession
    ) -> str | None:
        """Authenticate via the auth.neovac.ch portal.

        1. POST to auth.neovac.ch/api/v1/Account/Login
        2. Use established session cookies to get a myenergy token
        """
        # Step 1: Login to auth portal
        payload = {
            "email": self._email,
            "password": self._password,
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            async with session.post(
                AUTH_LOGIN_URL,
                json=payload,
                headers=headers,
            ) as resp:
                _LOGGER.debug(
                    "Auth portal login response: status=%s", resp.status
                )
                if resp.status not in (200, 204):
                    text = await resp.text()
                    _LOGGER.debug("Auth portal login failed: %s", text)
                    if resp.status in (401, 403):
                        raise NeoVacAuthError(
                            "Invalid email or password"
                        )
                    raise NeoVacApiError(
                        f"Auth portal returned status {resp.status}"
                    )

                # Try to get token from response body
                try:
                    data = await resp.json()
                    _LOGGER.debug("Auth portal response: %s", data)
                    token = None
                    if isinstance(data, dict):
                        token = (
                            data.get("sessionToken")
                            or data.get("token")
                            or data.get("accessToken")
                        )
                    elif isinstance(data, str):
                        token = data
                    if token:
                        return token
                except Exception:
                    pass

        except aiohttp.ClientError as err:
            raise NeoVacConnectionError(
                f"Cannot connect to auth portal: {err}"
            ) from err

        # Step 2: Check if we're authenticated via cookies
        try:
            async with session.get(
                AUTH_IS_AUTHENTICATED_URL,
                headers={"Accept": "application/json"},
            ) as resp:
                _LOGGER.debug(
                    "IsAuthenticated response: status=%s", resp.status
                )
                if resp.status == 200:
                    try:
                        data = await resp.json()
                        _LOGGER.debug("IsAuthenticated data: %s", data)
                    except Exception:
                        pass
        except Exception as err:
            _LOGGER.debug("IsAuthenticated check failed: %s", err)

        # Step 3: Try to authenticate to myenergy using the session cookies
        try:
            async with session.post(
                MYENERGY_AUTH_URL,
                json=payload,
                headers=headers,
            ) as resp:
                _LOGGER.debug(
                    "MyEnergy auth (with cookies): status=%s", resp.status
                )
                if resp.status == 200:
                    data = await resp.json()
                    token = data.get("sessionToken") or data.get("token")
                    if token:
                        return token
                    if isinstance(data, str):
                        return data
                    _LOGGER.debug("MyEnergy auth response: %s", data)
        except aiohttp.ClientError as err:
            _LOGGER.debug("MyEnergy auth with cookies failed: %s", err)

        return None

    def _auth_headers(self) -> dict[str, str]:
        """Return headers with Bearer token for authenticated requests."""
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _request(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> Any:
        """Make an authenticated API request.

        Handles token refresh on 401.
        """
        session = await self._ensure_session()

        if not self._token:
            await self.authenticate()

        headers = self._auth_headers()
        kwargs.setdefault("headers", headers)

        try:
            async with session.request(method, url, **kwargs) as resp:
                if resp.status == 401:
                    _LOGGER.debug("Got 401, re-authenticating")
                    self._token = None
                    await self.authenticate()
                    kwargs["headers"] = self._auth_headers()
                    async with session.request(
                        method, url, **kwargs
                    ) as retry_resp:
                        if retry_resp.status == 401:
                            raise NeoVacAuthError(
                                "Re-authentication failed"
                            )
                        retry_resp.raise_for_status()
                        return await retry_resp.json()

                if resp.status == 404:
                    return None

                resp.raise_for_status()
                try:
                    return await resp.json()
                except aiohttp.ContentTypeError:
                    text = await resp.text()
                    _LOGGER.debug(
                        "Non-JSON response from %s: %s", url, text[:200]
                    )
                    return None

        except NeoVacAuthError:
            raise
        except aiohttp.ClientError as err:
            raise NeoVacConnectionError(
                f"Connection error: {err}"
            ) from err
        except Exception as err:
            raise NeoVacApiError(
                f"API error: {err}"
            ) from err

    async def get_usage_units(self) -> list[dict[str, Any]]:
        """Get all usage units (metering points/apartments).

        Returns a list of usage unit objects with at least:
        - usageUnitId: unique identifier
        - customName: user-given name (may be None)
        """
        data = await self._request("GET", MYENERGY_USAGE_UNITS_URL)
        if data is None:
            return []
        if isinstance(data, list):
            return data
        # Some APIs wrap in an object
        if isinstance(data, dict):
            return data.get("usageUnits", data.get("items", [data]))
        return []

    async def get_usage_unit(self, unit_id: str) -> dict[str, Any] | None:
        """Get a single usage unit by ID."""
        url = f"{MYENERGY_USAGE_UNITS_URL}/{unit_id}"
        return await self._request("GET", url)

    async def get_invoice_periods(
        self, unit_id: str
    ) -> list[dict[str, Any]]:
        """Get invoice periods for a usage unit."""
        url = f"{MYENERGY_USAGE_UNITS_URL}/{unit_id}/invoiceperiod"
        data = await self._request("GET", url)
        if data is None:
            return []
        if isinstance(data, list):
            return data
        return []

    async def get_available_tabs(
        self,
        unit_id: str,
        category: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any] | None:
        """Get available tabs/features for a category.

        This tells us which data types are available for this usage unit.
        """
        if start_date is None or end_date is None:
            now = datetime.now()
            end_date = now.strftime("%Y-%m-%d %H:%M")
            start_date = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M")

        url = f"{MYENERGY_USAGE_UNITS_URL}/{unit_id}/tabs/{category}"
        params = {"startdate": start_date, "enddate": end_date}
        return await self._request("GET", url, params=params)

    async def get_consumption(
        self,
        unit_id: str,
        category: str,
        resolution: str = "Hour",
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any] | None:
        """Get consumption data for a usage unit and category.

        Args:
            unit_id: Usage unit ID.
            category: Energy category (Electricity, Water, etc.).
            resolution: Data resolution (Hour, Month, Year).
            start_date: Start date in 'YYYY-MM-DD HH:MM' format.
            end_date: End date in 'YYYY-MM-DD HH:MM' format.

        Returns:
            Consumption data dict or None if not available.
        """
        if start_date is None or end_date is None:
            now = datetime.now()
            end_date = now.strftime("%Y-%m-%d %H:%M")
            start_date = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")

        url = (
            f"{MYENERGY_USAGE_UNITS_URL}/{unit_id}/consumption/{category}"
        )
        params = {
            "resolution": resolution,
            "startdate": start_date,
            "enddate": end_date,
        }
        return await self._request("GET", url, params=params)

    async def get_comparison(
        self,
        unit_id: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any] | None:
        """Get comparison/home data for a usage unit.

        This provides an overview of all consumption categories.
        """
        if start_date is None or end_date is None:
            now = datetime.now()
            end_date = now.strftime("%Y-%m-%d %H:%M")
            start_date = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M")

        url = f"{MYENERGY_USAGE_UNITS_URL}/{unit_id}/compare"
        params = {"startdate": start_date, "enddate": end_date}
        return await self._request("GET", url, params=params)

    async def get_comparison_settings(
        self, unit_id: str
    ) -> dict[str, Any] | None:
        """Get comparison settings (tells us which categories are available)."""
        url = f"{MYENERGY_USAGE_UNITS_URL}/{unit_id}/comparisonsettings"
        return await self._request("GET", url)

    async def get_history(
        self,
        unit_id: str,
        category: str,
        resolution: str = "Hour",
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any] | None:
        """Get historical data for a usage unit and category."""
        if start_date is None or end_date is None:
            now = datetime.now()
            end_date = now.strftime("%Y-%m-%d %H:%M")
            start_date = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M")

        url = f"{MYENERGY_USAGE_UNITS_URL}/{unit_id}/history/{category}"
        params = {
            "resolution": resolution,
            "startdate": start_date,
            "enddate": end_date,
        }
        return await self._request("GET", url, params=params)

    async def get_available_categories(
        self, unit_id: str
    ) -> list[str]:
        """Determine which energy categories are available for a usage unit.

        Uses the comparison settings endpoint to discover available categories.
        Falls back to trying each category individually.
        """
        available = []

        # Try comparison settings first
        try:
            settings = await self.get_comparison_settings(unit_id)
            if settings and isinstance(settings, list):
                for item in settings:
                    cat = item.get("category")
                    if cat and cat in SUPPORTED_CATEGORIES:
                        available.append(cat)
                if available:
                    return available
            elif settings and isinstance(settings, dict):
                # May be a dict with category keys
                for cat in SUPPORTED_CATEGORIES:
                    if cat.lower() in {k.lower() for k in settings}:
                        available.append(cat)
                if available:
                    return available
        except Exception as err:
            _LOGGER.debug("Could not get comparison settings: %s", err)

        # Try the comparison endpoint
        try:
            comparison = await self.get_comparison(unit_id)
            if comparison:
                _LOGGER.debug(
                    "Comparison data keys: %s",
                    comparison.keys() if isinstance(comparison, dict) else type(comparison),
                )
                if isinstance(comparison, dict):
                    for cat in SUPPORTED_CATEGORIES:
                        if cat.lower() in {k.lower() for k in comparison}:
                            available.append(cat)
                elif isinstance(comparison, list):
                    for item in comparison:
                        if isinstance(item, dict):
                            cat = item.get("category") or item.get("type")
                            if cat and cat in SUPPORTED_CATEGORIES:
                                available.append(cat)
                if available:
                    return available
        except Exception as err:
            _LOGGER.debug("Could not get comparison data: %s", err)

        # Fallback: try each category individually
        for category in SUPPORTED_CATEGORIES:
            try:
                data = await self.get_consumption(unit_id, category)
                if data is not None:
                    available.append(category)
            except Exception:
                pass

        return available
