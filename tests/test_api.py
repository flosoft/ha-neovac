#!/usr/bin/env python3
"""Standalone CLI test script for the NeoVac MyEnergy API.

Tests authentication and data fetching without requiring Home Assistant.

Authentication flow (reverse-engineered from the Angular frontend):
1. POST /connect/challenge (no prompt, or prompt=none) to get the OIDC authorize URL
2. POST to auth.neovac.ch/api/v1/Account/Login with the OIDC authorize path as redirectUrl
3. Follow the redirect chain: authorize -> signin-oidc -> session cookie set
4. Use cookie-based auth for all API calls

Usage:
    python tests/test_api.py --email user@example.com --password secret
    python tests/test_api.py  # reads from NEOVAC_EMAIL and NEOVAC_PASSWORD env vars

Options:
    --email       NeoVac account email
    --password    NeoVac account password
    --unit-id     Specific usage unit ID to query (optional)
    --verbose     Enable debug logging
    --dump-raw    Dump raw API responses as JSON files
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

import aiohttp

# Import constants directly from const.py to avoid loading the HA-dependent
# __init__.py of the custom_components.neovac package.
import importlib.util

_const_path = Path(__file__).parent.parent / "custom_components" / "neovac" / "const.py"
_spec = importlib.util.spec_from_file_location("neovac_const", _const_path)
_const = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_const)

AUTH_BASE_URL = _const.AUTH_BASE_URL
AUTH_IS_AUTHENTICATED_URL = _const.AUTH_IS_AUTHENTICATED_URL
AUTH_LOGIN_URL = _const.AUTH_LOGIN_URL
MYENERGY_BASE_URL = _const.MYENERGY_BASE_URL
MYENERGY_API_URL = _const.MYENERGY_API_URL
MYENERGY_CHALLENGE_URL = _const.MYENERGY_CHALLENGE_URL
MYENERGY_USAGE_UNITS_URL = _const.MYENERGY_USAGE_UNITS_URL
CATEGORY_ELECTRICITY = _const.CATEGORY_ELECTRICITY
RESOLUTION_HOUR = _const.RESOLUTION_HOUR
RESOLUTION_QUARTER_HOUR = _const.RESOLUTION_QUARTER_HOUR
SUPPORTED_CATEGORIES = _const.SUPPORTED_CATEGORIES

# Derived
MYENERGY_ENVIRONMENT_URL = f"{MYENERGY_API_URL}/environment"


def setup_logging(verbose: bool = False) -> None:
    """Configure logging."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def print_json(label: str, data: object) -> None:
    """Pretty-print a JSON-serializable object."""
    print(f"\n{'=' * 60}")
    print(f"  {label}")
    print(f"{'=' * 60}")
    print(json.dumps(data, indent=2, default=str, ensure_ascii=False))


def dump_to_file(filename: str, data: object) -> None:
    """Dump data to a JSON file in the current directory."""
    path = Path(f"debug_{filename}.json")
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str, ensure_ascii=False)
    print(f"  -> Dumped to {path}")


def print_cookies(jar: aiohttp.CookieJar) -> None:
    """Print all cookies in the jar."""
    count = 0
    for cookie in jar:
        count += 1
        val = cookie.value
        if len(val) > 40:
            val = val[:20] + "..." + val[-8:]
        domain = cookie.get("domain", "N/A")
        print(f"    {cookie.key} = {val}  (domain: {domain})")
    if count == 0:
        print("    (none)")


async def authenticate(
    session: aiohttp.ClientSession,
    email: str,
    password: str,
    dump_raw: bool = False,
) -> bool:
    """Authenticate and establish a session for myenergy.neovac.ch.

    Returns True if authentication succeeded (cookie-based session established).

    Flow:
    1. Trigger OIDC challenge on myenergy to get the authorize URL
    2. Login to auth.neovac.ch with the OIDC authorize path as redirectUrl
    3. Follow the redirect chain to complete the OIDC code exchange
    """
    log = logging.getLogger("auth")

    # ── Step 1: Trigger OIDC challenge to get the authorize URL ──
    # We use prompt=none here because we haven't logged in yet -- we just
    # want to get the authorize URL structure. The challenge always returns
    # 401 with Location regardless of auth state.
    log.info("Step 1: Trigger OIDC challenge to get authorize URL")
    challenge_url = f"{MYENERGY_CHALLENGE_URL}?prompt=login"
    log.info("POST %s", challenge_url)

    authorize_url = None
    try:
        async with session.post(
            challenge_url,
            allow_redirects=False,
            headers={
                "Accept": "application/json, text/html",
                "Origin": MYENERGY_BASE_URL,
                "Referer": f"{MYENERGY_BASE_URL}/",
            },
        ) as resp:
            log.info("  Status: %s", resp.status)
            authorize_url = resp.headers.get("Location")
            if authorize_url:
                log.info("  Got authorize URL: %s", authorize_url[:100] + "...")
            else:
                log.error("  No Location header in challenge response")
                return False
    except aiohttp.ClientError as err:
        log.error("  Challenge error: %s", err)
        return False

    # ── Step 2: Login to auth.neovac.ch ──────────────────────────
    # The authorize URL points to auth.neovac.ch/connect/authorize?...
    # When we hit it unauthenticated, it redirects to the login page.
    # Instead, we first login via the API, then hit the authorize URL.
    log.info("Step 2: Login to auth portal")
    log.info("POST %s", AUTH_LOGIN_URL)

    # Extract the path from the authorize URL to use as redirectUrl
    parsed_auth = urlparse(authorize_url)
    # The redirectUrl for the login API should be the path+query of the authorize URL
    authorize_path = parsed_auth.path
    if parsed_auth.query:
        authorize_path += "?" + parsed_auth.query

    payload = {
        "username": email,
        "password": password,
        "redirectUrl": authorize_path,
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": AUTH_BASE_URL,
        "Referer": f"{AUTH_BASE_URL}/",
    }

    try:
        async with session.post(
            AUTH_LOGIN_URL, json=payload, headers=headers,
            allow_redirects=False,
        ) as resp:
            log.info("  Status: %s", resp.status)
            log.debug("  Response headers: %s", dict(resp.headers))

            body = None
            try:
                body = await resp.json()
            except Exception:
                body = await resp.text()

            if dump_raw:
                dump_to_file("01_auth_login", body)

            if resp.status not in (200, 204, 301, 302):
                log.error("  Auth portal login FAILED: %s", body)
                return False

            log.info("  Auth portal login OK")
            log.info("  Response: %s", body)

            # The response should contain a redirectUrl pointing to the
            # authorize endpoint (same path we sent)
            redirect_url = None
            if isinstance(body, dict):
                redirect_url = body.get("redirectUrl")

    except aiohttp.ClientError as err:
        log.error("  Connection error: %s", err)
        return False

    log.info("  Cookies after login:")
    print_cookies(session.cookie_jar)

    # ── Step 3: Follow the OIDC authorize flow ───────────────────
    # Now we're authenticated at auth.neovac.ch (have cookies).
    # Hit the authorize URL -- it should auto-complete and redirect
    # us back to myenergy.neovac.ch/signin-oidc with a code.
    log.info("Step 3: Follow OIDC authorize flow")

    # Build full authorize URL (either from login response or original)
    if redirect_url and redirect_url != "/":
        # The login response gave us the path back
        if redirect_url.startswith("/"):
            next_url = f"{AUTH_BASE_URL}{redirect_url}"
        else:
            next_url = redirect_url
    else:
        next_url = authorize_url

    log.info("  Following: %s", next_url[:120] + "..." if len(next_url) > 120 else next_url)

    # Follow the redirect chain manually so we can log each step
    success = await _follow_redirect_chain(session, next_url, dump_raw, log)

    log.info("  Cookies after OIDC flow:")
    print_cookies(session.cookie_jar)

    if not success:
        # ── Fallback: Try using prompt=none ──────────────────────
        log.info("Step 3 (fallback): Try challenge with prompt=none")
        challenge_url_none = f"{MYENERGY_CHALLENGE_URL}?prompt=none"
        try:
            async with session.post(
                challenge_url_none,
                allow_redirects=False,
                headers={
                    "Accept": "application/json, text/html",
                    "Origin": MYENERGY_BASE_URL,
                },
            ) as resp:
                log.info("  Status: %s", resp.status)
                location = resp.headers.get("Location")
                if location:
                    log.info("  Location: %s", location[:120])
                    success = await _follow_redirect_chain(session, location, dump_raw, log)
        except Exception as err:
            log.warning("  Fallback challenge error: %s", err)

    # ── Step 4: Test API access ──────────────────────────────────
    log.info("Step 4: Test cookie-based API access")
    try:
        async with session.get(
            MYENERGY_USAGE_UNITS_URL,
            headers={"Accept": "application/json"},
        ) as resp:
            log.info("  GET %s -> %s", MYENERGY_USAGE_UNITS_URL, resp.status)
            if resp.status == 200:
                log.info("  Cookie-based API access works!")
                return True
            else:
                text = await resp.text()
                log.info("  API returned %s: %s", resp.status, text[:300])
    except Exception as err:
        log.warning("  API test error: %s", err)

    return False


async def _follow_redirect_chain(
    session: aiohttp.ClientSession,
    url: str,
    dump_raw: bool,
    log: logging.Logger,
    max_redirects: int = 15,
) -> bool:
    """Follow a redirect chain manually, logging each step.

    Returns True if we end up with a successful (non-auth) page.
    """
    for i in range(max_redirects):
        log.info("  Redirect %d: GET %s", i + 1, url[:120] + ("..." if len(url) > 120 else ""))
        try:
            async with session.get(
                url,
                allow_redirects=False,
                headers={"Accept": "text/html,application/json"},
            ) as resp:
                log.info("    Status: %s", resp.status)

                location = resp.headers.get("Location")

                if resp.status in (301, 302, 303, 307, 308):
                    if location:
                        # Resolve relative URLs
                        if location.startswith("/"):
                            parsed_current = urlparse(url)
                            location = f"{parsed_current.scheme}://{parsed_current.netloc}{location}"
                        log.info("    -> %s", location[:120] + ("..." if len(location) > 120 else ""))
                        url = location
                        continue
                    else:
                        log.warning("    Redirect with no Location header")
                        return False

                if resp.status == 200:
                    # Check final URL - if we're on myenergy, we might be done
                    final_url = str(resp.url)
                    log.info("    Final URL: %s", final_url)

                    # If we landed on the login page, auth didn't auto-complete
                    if "/auth/login" in final_url:
                        log.warning("    Landed on login page - OIDC did not auto-complete")
                        if dump_raw:
                            try:
                                body = await resp.text()
                                dump_to_file(f"redirect_{i+1}_login_page", {"url": final_url, "body": body[:2000]})
                            except Exception:
                                pass
                        return False

                    # If we're on myenergy.neovac.ch (not the auth portal), we're done
                    if "myenergy.neovac.ch" in final_url:
                        log.info("    Landed on myenergy - OIDC flow complete!")
                        return True

                    log.info("    Landed on: %s", final_url)
                    if dump_raw:
                        try:
                            body = await resp.text()
                            dump_to_file(f"redirect_{i+1}_final", {"url": final_url, "status": resp.status, "body": body[:2000]})
                        except Exception:
                            pass
                    return False

                if resp.status == 401:
                    if location:
                        log.info("    401 with Location, following...")
                        if location.startswith("/"):
                            parsed_current = urlparse(url)
                            location = f"{parsed_current.scheme}://{parsed_current.netloc}{location}"
                        url = location
                        continue
                    else:
                        log.warning("    401 with no Location")
                        return False

                log.warning("    Unexpected status: %s", resp.status)
                return False

        except Exception as err:
            log.warning("    Redirect error: %s", err)
            return False

    log.warning("    Too many redirects")
    return False


async def fetch_data(
    session: aiohttp.ClientSession,
    url: str,
    label: str,
    dump_raw: bool = False,
    dump_name: str | None = None,
) -> dict | list | None:
    """Fetch JSON data from a URL using the session cookies."""
    log = logging.getLogger(label)
    headers = {"Accept": "application/json"}

    log.info("GET %s", url)
    try:
        async with session.get(url, headers=headers) as resp:
            log.info("  Status: %s", resp.status)
            if resp.status == 200:
                data = await resp.json()
                if dump_raw and dump_name:
                    dump_to_file(dump_name, data)
                return data
            elif resp.status == 404:
                log.info("  Not found")
                return None
            else:
                text = await resp.text()
                log.warning("  Failed: %s", text[:500])
                return None
    except Exception as err:
        log.error("  Error: %s", err)
        return None


async def main() -> None:
    """Run the test script."""
    parser = argparse.ArgumentParser(
        description="Test NeoVac MyEnergy API connectivity and data parsing"
    )
    parser.add_argument(
        "--email",
        default=os.environ.get("NEOVAC_EMAIL"),
        help="NeoVac account email (or set NEOVAC_EMAIL env var)",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("NEOVAC_PASSWORD"),
        help="NeoVac account password (or set NEOVAC_PASSWORD env var)",
    )
    parser.add_argument(
        "--unit-id",
        default=None,
        help="Specific usage unit ID to query",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--dump-raw",
        action="store_true",
        help="Dump raw API responses as JSON files",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)
    log = logging.getLogger("main")

    if not args.email or not args.password:
        print("Error: Email and password are required.")
        print("  Use --email and --password, or set NEOVAC_EMAIL / NEOVAC_PASSWORD")
        sys.exit(1)

    print(f"\nNeoVac MyEnergy API Test")
    print(f"Email: {args.email}")
    print(f"Time:  {datetime.now().isoformat()}")
    print()

    # Use a cookie jar that handles cross-domain cookies
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=jar) as session:

        # ── Phase 1: Authentication ──────────────────────────────
        print("\n" + "─" * 60)
        print("  PHASE 1: Authentication")
        print("─" * 60)

        ok = await authenticate(session, args.email, args.password, args.dump_raw)

        if not ok:
            print("\n*** AUTHENTICATION FAILED ***")
            print("Could not establish an authenticated session.")
            print("\nCookies in jar:")
            print_cookies(jar)
            sys.exit(1)

        print("\n  Authentication: OK (cookie-based)")

        # ── Phase 2: Fetch Usage Units ───────────────────────────
        print("\n" + "─" * 60)
        print("  PHASE 2: Usage Units")
        print("─" * 60)

        units_data = await fetch_data(
            session, MYENERGY_USAGE_UNITS_URL,
            "usage_units", args.dump_raw, "usage_units",
        )
        units = []
        if isinstance(units_data, list):
            units = units_data
        elif isinstance(units_data, dict):
            units = [units_data]

        if not units:
            print("\n*** No usage units found ***")
            print_json("Raw response", units_data)
            sys.exit(1)

        print_json("Usage Units", units)

        # Select unit to test
        unit_id = args.unit_id
        if not unit_id:
            unit = units[0]
            unit_id = str(
                unit.get("usageUnitId")
                or unit.get("id")
                or unit.get("unitId")
            )
            unit_name = unit.get("customName") or unit.get("name") or "unnamed"
            print(f"\n  Using first unit: {unit_name} (ID: {unit_id})")

        # ── Phase 3: Invoice Periods ─────────────────────────────
        print("\n" + "─" * 60)
        print("  PHASE 3: Invoice Periods")
        print("─" * 60)

        periods = await fetch_data(
            session,
            f"{MYENERGY_USAGE_UNITS_URL}/{unit_id}/invoiceperiod",
            "invoice_periods", args.dump_raw, "invoice_periods",
        )
        if periods:
            print_json("Invoice Periods", periods)

        # ── Phase 4: Comparison Settings ─────────────────────────
        print("\n" + "─" * 60)
        print("  PHASE 4: Comparison Settings (Available Categories)")
        print("─" * 60)

        settings = await fetch_data(
            session,
            f"{MYENERGY_USAGE_UNITS_URL}/{unit_id}/comparisonsettings",
            "comparison_settings", args.dump_raw, "comparison_settings",
        )
        if settings:
            print_json("Comparison Settings", settings)

        # ── Phase 5: Comparison/Overview Data ────────────────────
        print("\n" + "─" * 60)
        print("  PHASE 5: Comparison Overview")
        print("─" * 60)

        now = datetime.now()
        end_date = now.strftime("%Y-%m-%d %H:%M")
        start_30d = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M")
        start_1d = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")

        comparison = await fetch_data(
            session,
            f"{MYENERGY_USAGE_UNITS_URL}/{unit_id}/compare?startdate={start_30d}&enddate={end_date}",
            "comparison", args.dump_raw, "comparison",
        )
        if comparison:
            print_json("Comparison Data", comparison)

        # ── Phase 6: Consumption Data per Category ───────────────
        print("\n" + "─" * 60)
        print("  PHASE 6: Consumption Data (per category)")
        print("─" * 60)

        available_categories = []
        for category in SUPPORTED_CATEGORIES:
            # Use QuarterHourly for Electricity, Hourly for everything else
            resolution = (
                RESOLUTION_QUARTER_HOUR
                if category == CATEGORY_ELECTRICITY
                else RESOLUTION_HOUR
            )
            print(f"\n  Testing category: {category} (resolution: {resolution})")
            url = (
                f"{MYENERGY_USAGE_UNITS_URL}/{unit_id}/consumption/{category}"
                f"?resolution={resolution}"
                f"&startdate={start_1d}&enddate={end_date}"
            )
            data = await fetch_data(
                session, url, "consumption",
                args.dump_raw, f"consumption_{category}",
            )
            if data is not None:
                available_categories.append(category)
                print_json(f"Consumption: {category}", data)

        # ── Summary ──────────────────────────────────────────────
        print("\n" + "=" * 60)
        print("  SUMMARY")
        print("=" * 60)
        print(f"  Authentication: OK (cookie-based)")
        print(f"  Usage units found: {len(units)}")
        for unit in units:
            uid = unit.get("usageUnitId") or unit.get("id")
            name = unit.get("customName") or unit.get("name") or "unnamed"
            print(f"    - {name} (ID: {uid})")
        print(f"  Invoice periods: {len(periods) if isinstance(periods, list) else 'N/A'}")
        print(f"  Available categories: {available_categories}")
        print(f"  Categories tested: {len(SUPPORTED_CATEGORIES)}")
        print(f"  Categories with data: {len(available_categories)}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
