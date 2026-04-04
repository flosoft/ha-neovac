"""NeoVac MyEnergy API client.

Handles authentication via OIDC flow (auth.neovac.ch <-> myenergy.neovac.ch)
and data fetching from the myenergy API.

Authentication flow:
1. POST /connect/challenge on myenergy to get the OIDC authorize URL
2. POST /api/v1/Account/Login on auth.neovac.ch with credentials + authorize path
3. Follow the OIDC redirect chain to complete the code exchange
4. Session cookies are set on myenergy.neovac.ch for subsequent API calls
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import aiohttp

from .const import (
    AUTH_BASE_URL,
    AUTH_LOGIN_URL,
    CATEGORY_ELECTRICITY,
    MYENERGY_BASE_URL,
    MYENERGY_CHALLENGE_URL,
    MYENERGY_USAGE_UNITS_URL,
    RESOLUTION_HOUR,
    RESOLUTION_QUARTER_HOUR,
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
    """Client to interact with the NeoVac MyEnergy API.

    Uses cookie-based authentication via OIDC flow.
    """

    def __init__(
        self,
        email: str,
        password: str,
    ) -> None:
        """Initialize the API client."""
        self._email = email
        self._password = password
        self._session: aiohttp.ClientSession | None = None
        self._authenticated = False

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Ensure we have an active aiohttp session with cross-domain cookies."""
        if self._session is None or self._session.closed:
            jar = aiohttp.CookieJar(unsafe=True)
            self._session = aiohttp.ClientSession(cookie_jar=jar)
            self._authenticated = False
        return self._session

    async def close(self) -> None:
        """Close the session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
            self._authenticated = False

    @property
    def is_authenticated(self) -> bool:
        """Return whether we have an active authenticated session."""
        return self._authenticated

    async def authenticate(self) -> bool:
        """Authenticate with NeoVac via OIDC flow.

        1. Trigger OIDC challenge on myenergy to get the authorize URL
        2. Login to auth.neovac.ch with the authorize path as redirectUrl
        3. Follow the redirect chain to complete OIDC code exchange
        4. Verify API access works

        Returns True if authentication succeeded.
        Raises NeoVacAuthError on failure.
        """
        session = await self._ensure_session()

        # Step 1: Trigger OIDC challenge to get the authorize URL
        authorize_url = await self._get_authorize_url(session)
        if not authorize_url:
            raise NeoVacAuthError("Failed to get OIDC authorize URL")

        # Step 2: Login to auth portal with the authorize path as redirectUrl
        authorize_path = await self._login_auth_portal(
            session, authorize_url
        )
        if not authorize_path:
            raise NeoVacAuthError("Invalid email or password")

        # Step 3: Follow the OIDC redirect chain
        success = await self._complete_oidc_flow(
            session, authorize_path
        )
        if not success:
            raise NeoVacAuthError(
                "OIDC flow failed - could not establish session"
            )

        # Step 4: Verify API access
        try:
            async with session.get(
                MYENERGY_USAGE_UNITS_URL,
                headers={"Accept": "application/json"},
            ) as resp:
                if resp.status == 200:
                    self._authenticated = True
                    _LOGGER.debug("Authentication successful")
                    return True
                else:
                    raise NeoVacAuthError(
                        f"API access check failed with status {resp.status}"
                    )
        except aiohttp.ClientError as err:
            raise NeoVacConnectionError(
                f"Cannot connect to myenergy API: {err}"
            ) from err

    async def _get_authorize_url(
        self, session: aiohttp.ClientSession
    ) -> str | None:
        """Trigger OIDC challenge to get the authorize URL."""
        challenge_url = f"{MYENERGY_CHALLENGE_URL}?prompt=login"
        try:
            async with session.post(
                challenge_url,
                allow_redirects=False,
                headers={
                    "Accept": "application/json, text/html",
                    "Origin": MYENERGY_BASE_URL,
                },
            ) as resp:
                location = resp.headers.get("Location")
                if location:
                    _LOGGER.debug("Got authorize URL from challenge")
                    return location
                _LOGGER.debug(
                    "Challenge returned %s with no Location", resp.status
                )
                return None
        except aiohttp.ClientError as err:
            _LOGGER.debug("Challenge request failed: %s", err)
            return None

    async def _login_auth_portal(
        self, session: aiohttp.ClientSession, authorize_url: str
    ) -> str | None:
        """Login to auth.neovac.ch and return the authorize path.

        Returns the redirectUrl from the login response (the OIDC authorize
        path), or None if login failed.
        """
        # Extract the path from the authorize URL to use as redirectUrl
        parsed = urlparse(authorize_url)
        authorize_path = parsed.path
        if parsed.query:
            authorize_path += "?" + parsed.query

        payload = {
            "username": self._email,
            "password": self._password,
            "redirectUrl": authorize_path,
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": AUTH_BASE_URL,
        }

        try:
            async with session.post(
                AUTH_LOGIN_URL,
                json=payload,
                headers=headers,
                allow_redirects=False,
            ) as resp:
                if resp.status in (401, 403):
                    raise NeoVacAuthError("Invalid email or password")

                if resp.status not in (200, 204):
                    body = await resp.text()
                    _LOGGER.debug(
                        "Auth portal returned %s: %s", resp.status, body[:300]
                    )
                    # Check for specific error codes
                    try:
                        data = await resp.json()
                        error_code = data.get("ErrorCode", "")
                        if error_code == "WrongPassword":
                            raise NeoVacAuthError("Invalid password")
                        if error_code == "UnknownUser":
                            raise NeoVacAuthError("Unknown user")
                        if error_code == "IsLockedOut":
                            raise NeoVacAuthError("Account is locked")
                    except Exception:
                        pass
                    return None

                try:
                    data = await resp.json()
                    redirect_url = data.get("redirectUrl")
                    if redirect_url and redirect_url != "/":
                        return redirect_url
                    # If redirectUrl is "/" it means our authorize path
                    # wasn't accepted; fall back to the original
                    return authorize_path
                except Exception:
                    return authorize_path

        except NeoVacAuthError:
            raise
        except aiohttp.ClientError as err:
            raise NeoVacConnectionError(
                f"Cannot connect to auth portal: {err}"
            ) from err

    async def _complete_oidc_flow(
        self,
        session: aiohttp.ClientSession,
        authorize_path: str,
        max_redirects: int = 15,
    ) -> bool:
        """Follow the OIDC redirect chain to establish a session.

        Starting from the authorize endpoint on auth.neovac.ch, follows
        redirects through code exchange and signin-oidc callback until
        we land on myenergy.neovac.ch with session cookies set.
        """
        # Build full URL
        if authorize_path.startswith("/"):
            url = f"{AUTH_BASE_URL}{authorize_path}"
        else:
            url = authorize_path

        for i in range(max_redirects):
            _LOGGER.debug("OIDC redirect %d: %s", i + 1, url[:100])
            try:
                async with session.get(
                    url,
                    allow_redirects=False,
                    headers={"Accept": "text/html,application/json"},
                ) as resp:
                    location = resp.headers.get("Location")

                    if resp.status in (301, 302, 303, 307, 308):
                        if not location:
                            _LOGGER.debug("Redirect with no Location")
                            return False
                        if location.startswith("/"):
                            parsed = urlparse(url)
                            location = (
                                f"{parsed.scheme}://{parsed.netloc}{location}"
                            )
                        url = location
                        continue

                    if resp.status == 401 and location:
                        if location.startswith("/"):
                            parsed = urlparse(url)
                            location = (
                                f"{parsed.scheme}://{parsed.netloc}{location}"
                            )
                        url = location
                        continue

                    if resp.status == 200:
                        final_url = str(resp.url)
                        if "/auth/login" in final_url:
                            _LOGGER.debug(
                                "Landed on login page - trying prompt=none"
                            )
                            # Try with prompt=none as fallback
                            return await self._try_prompt_none(session)
                        if "myenergy.neovac.ch" in final_url:
                            _LOGGER.debug("OIDC flow complete")
                            return True
                        _LOGGER.debug("Landed on unexpected page: %s", final_url)
                        return False

                    _LOGGER.debug("Unexpected status %s", resp.status)
                    return False

            except aiohttp.ClientError as err:
                _LOGGER.debug("OIDC redirect error: %s", err)
                return False

        _LOGGER.debug("Too many OIDC redirects")
        return False

    async def _try_prompt_none(
        self, session: aiohttp.ClientSession
    ) -> bool:
        """Try OIDC challenge with prompt=none as a fallback."""
        challenge_url = f"{MYENERGY_CHALLENGE_URL}?prompt=none"
        try:
            async with session.post(
                challenge_url,
                allow_redirects=False,
                headers={
                    "Accept": "application/json, text/html",
                    "Origin": MYENERGY_BASE_URL,
                },
            ) as resp:
                location = resp.headers.get("Location")
                if location:
                    return await self._complete_oidc_flow(
                        session, location, max_redirects=10
                    )
        except aiohttp.ClientError as err:
            _LOGGER.debug("prompt=none challenge failed: %s", err)
        return False

    async def _request(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> Any:
        """Make an authenticated API request.

        Handles re-authentication on 401.
        """
        session = await self._ensure_session()

        if not self._authenticated:
            await self.authenticate()

        headers = {"Accept": "application/json"}
        kwargs.setdefault("headers", headers)

        try:
            async with session.request(method, url, **kwargs) as resp:
                if resp.status == 401:
                    _LOGGER.debug("Got 401, re-authenticating")
                    self._authenticated = False
                    await self.authenticate()
                    async with session.request(
                        method, url, **kwargs
                    ) as retry_resp:
                        if retry_resp.status == 401:
                            raise NeoVacAuthError(
                                "Re-authentication failed"
                            )
                        retry_resp.raise_for_status()
                        try:
                            return await retry_resp.json()
                        except aiohttp.ContentTypeError:
                            return None

                if resp.status == 404:
                    return None

                resp.raise_for_status()
                try:
                    return await resp.json()
                except aiohttp.ContentTypeError:
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

        Each unit has:
        - usageUnitId: int
        - city, street, streetNumber: address
        - contractNumber: str
        - flatNumber: str
        - customName: str (user-given name)
        - hasConsumptions: bool
        - allowUsagesAccess: bool
        """
        data = await self._request("GET", MYENERGY_USAGE_UNITS_URL)
        if data is None:
            return []
        if isinstance(data, list):
            return data
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
        """Get invoice periods for a usage unit.

        Each period has: invoicePeriodId, startDate, endDate.
        """
        url = f"{MYENERGY_USAGE_UNITS_URL}/{unit_id}/invoiceperiod"
        data = await self._request("GET", url)
        if data is None:
            return []
        if isinstance(data, list):
            return data
        return []

    async def get_consumption(
        self,
        unit_id: str,
        category: str,
        resolution: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any] | None:
        """Get consumption data for a usage unit and category.

        Args:
            unit_id: Usage unit ID.
            category: Energy category (Electricity, Water, etc.).
            resolution: Data resolution. If None, uses the finest available
                        (QuarterHourly for Electricity, Hourly for others).
            start_date: Start date in 'YYYY-MM-DD HH:MM' format.
            end_date: End date in 'YYYY-MM-DD HH:MM' format.

        Returns:
            Consumption data dict with:
            - measurementUnit: "KiloWattHours" | "Liter"
            - invoicePeriods: [{invoicePeriodId, startDate, endDate, sum, sumUnit}]
            - currentPeriodValues: [{date, value, isInterpolated}]
            - previousPeriodValues: [{date, value, isInterpolated}]
            - resolutions: ["Monthly", "Daily", "Hourly"|"QuarterHourly"]
        """
        if resolution is None:
            resolution = (
                RESOLUTION_QUARTER_HOUR
                if category == CATEGORY_ELECTRICITY
                else RESOLUTION_HOUR
            )

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

    async def get_comparison_settings(
        self, unit_id: str
    ) -> list[dict[str, Any]]:
        """Get comparison settings (tells us which categories are available).

        Returns a list of:
        [{"category": "Heating", "settings": {...}}, ...]
        """
        url = f"{MYENERGY_USAGE_UNITS_URL}/{unit_id}/comparisonsettings"
        data = await self._request("GET", url)
        if isinstance(data, list):
            return data
        return []

    async def get_available_categories(
        self, unit_id: str
    ) -> list[str]:
        """Determine which energy categories are available for a usage unit.

        Uses the comparison settings endpoint first, then falls back to
        trying each category individually via the consumption endpoint.
        """
        available = []

        # Try comparison settings first -- this is the most reliable
        try:
            settings = await self.get_comparison_settings(unit_id)
            for item in settings:
                cat = item.get("category")
                if cat and cat in SUPPORTED_CATEGORIES:
                    available.append(cat)
            if available:
                _LOGGER.debug(
                    "Categories from comparison settings: %s", available
                )
                # Also check WaterWarm/WaterCold which may not appear
                # in comparison settings but are available as sub-categories
                if "Water" in available:
                    for sub_cat in ("WaterWarm", "WaterCold"):
                        if sub_cat not in available:
                            try:
                                data = await self.get_consumption(
                                    unit_id, sub_cat
                                )
                                if data is not None:
                                    available.append(sub_cat)
                            except Exception:
                                pass
                return available
        except Exception as err:
            _LOGGER.debug("Could not get comparison settings: %s", err)

        # Fallback: try each category individually
        for category in SUPPORTED_CATEGORIES:
            try:
                data = await self.get_consumption(unit_id, category)
                if data is not None:
                    available.append(category)
            except Exception:
                pass

        return available
