#!/usr/bin/env python3
"""Standalone CLI test script for the NeoVac MyEnergy API.

Tests authentication and data fetching without requiring Home Assistant.

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

import aiohttp

# Allow importing from the custom_components directory
sys.path.insert(0, str(Path(__file__).parent.parent))

from custom_components.neovac.const import (
    AUTH_IS_AUTHENTICATED_URL,
    AUTH_LOGIN_URL,
    MYENERGY_AUTH_URL,
    MYENERGY_API_URL,
    MYENERGY_USAGE_UNITS_URL,
    RESOLUTION_HOUR,
    SUPPORTED_CATEGORIES,
)


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


async def test_auth_portal(
    session: aiohttp.ClientSession,
    email: str,
    password: str,
    dump_raw: bool = False,
) -> dict:
    """Test authentication via auth.neovac.ch portal."""
    log = logging.getLogger("auth_portal")

    result = {
        "success": False,
        "login_status": None,
        "login_response": None,
        "is_authenticated": None,
        "cookies": {},
    }

    # Step 1: Login
    log.info("POST %s", AUTH_LOGIN_URL)
    payload = {"email": email, "password": password}
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    async with session.post(
        AUTH_LOGIN_URL, json=payload, headers=headers
    ) as resp:
        result["login_status"] = resp.status
        log.info("  Status: %s", resp.status)
        log.info("  Headers: %s", dict(resp.headers))

        try:
            data = await resp.json()
            result["login_response"] = data
            if dump_raw:
                dump_to_file("auth_login", data)
        except Exception:
            text = await resp.text()
            result["login_response"] = text
            log.info("  Body (text): %s", text[:500])

        if resp.status in (200, 204):
            result["success"] = True
            log.info("  Auth portal login succeeded!")
        else:
            log.error("  Auth portal login FAILED")

    # Capture cookies
    for cookie in session.cookie_jar:
        result["cookies"][cookie.key] = {
            "value": cookie.value[:20] + "..." if len(cookie.value) > 20 else cookie.value,
            "domain": cookie.get("domain", ""),
            "path": cookie.get("path", ""),
            "secure": cookie.get("secure", ""),
        }

    if result["cookies"]:
        log.info("  Cookies received: %s", list(result["cookies"].keys()))
    else:
        log.warning("  No cookies received!")

    # Step 2: Check IsAuthenticated
    log.info("GET %s", AUTH_IS_AUTHENTICATED_URL)
    try:
        async with session.get(
            AUTH_IS_AUTHENTICATED_URL,
            headers={"Accept": "application/json"},
        ) as resp:
            result["is_authenticated_status"] = resp.status
            log.info("  Status: %s", resp.status)
            try:
                data = await resp.json()
                result["is_authenticated"] = data
                log.info("  Response: %s", data)
                if dump_raw:
                    dump_to_file("auth_is_authenticated", data)
            except Exception:
                text = await resp.text()
                result["is_authenticated"] = text
                log.info("  Body (text): %s", text[:500])
    except Exception as err:
        log.error("  IsAuthenticated error: %s", err)

    return result


async def test_myenergy_auth(
    session: aiohttp.ClientSession,
    email: str,
    password: str,
    dump_raw: bool = False,
) -> str | None:
    """Test direct authentication to myenergy API."""
    log = logging.getLogger("myenergy_auth")

    log.info("POST %s", MYENERGY_AUTH_URL)
    payload = {"email": email, "password": password}
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        async with session.post(
            MYENERGY_AUTH_URL, json=payload, headers=headers
        ) as resp:
            log.info("  Status: %s", resp.status)
            log.info("  Headers: %s", dict(resp.headers))

            try:
                data = await resp.json()
                log.info("  Response type: %s", type(data).__name__)
                if dump_raw:
                    dump_to_file("myenergy_auth", data)

                if resp.status == 200:
                    if isinstance(data, dict):
                        token = (
                            data.get("sessionToken")
                            or data.get("token")
                            or data.get("accessToken")
                        )
                        if token:
                            log.info(
                                "  Got token: %s...%s",
                                token[:8],
                                token[-4:] if len(token) > 12 else "",
                            )
                            return token
                        log.info("  Response keys: %s", list(data.keys()))
                    elif isinstance(data, str) and len(data) > 10:
                        log.info("  Got string token: %s...", data[:8])
                        return data
                    log.info("  Full response: %s", data)
                else:
                    log.warning("  Auth failed: %s", data)
            except Exception:
                text = await resp.text()
                log.info("  Body (text): %s", text[:500])
                if resp.status == 200 and len(text) > 10:
                    return text

    except aiohttp.ClientError as err:
        log.error("  Connection error: %s", err)

    return None


async def test_usage_units(
    session: aiohttp.ClientSession,
    token: str,
    dump_raw: bool = False,
) -> list[dict]:
    """Fetch and display usage units."""
    log = logging.getLogger("usage_units")

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    log.info("GET %s", MYENERGY_USAGE_UNITS_URL)
    async with session.get(
        MYENERGY_USAGE_UNITS_URL, headers=headers
    ) as resp:
        log.info("  Status: %s", resp.status)
        if resp.status != 200:
            text = await resp.text()
            log.error("  Failed: %s", text[:500])
            return []

        data = await resp.json()
        if dump_raw:
            dump_to_file("usage_units", data)

        units = data if isinstance(data, list) else [data]
        print_json("Usage Units", units)
        return units


async def test_consumption(
    session: aiohttp.ClientSession,
    token: str,
    unit_id: str,
    category: str,
    dump_raw: bool = False,
) -> dict | None:
    """Fetch consumption data for a specific category."""
    log = logging.getLogger("consumption")

    now = datetime.now()
    end_date = now.strftime("%Y-%m-%d %H:%M")
    start_date = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")

    url = f"{MYENERGY_USAGE_UNITS_URL}/{unit_id}/consumption/{category}"
    params = {
        "resolution": RESOLUTION_HOUR,
        "startdate": start_date,
        "enddate": end_date,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    log.info("GET %s (params: %s)", url, params)
    try:
        async with session.get(url, params=params, headers=headers) as resp:
            log.info("  Status: %s", resp.status)
            if resp.status == 200:
                data = await resp.json()
                if dump_raw:
                    dump_to_file(f"consumption_{category}", data)
                return data
            elif resp.status == 404:
                log.info("  Category %s not available for this unit", category)
                return None
            else:
                text = await resp.text()
                log.warning("  Unexpected response: %s", text[:500])
                return None
    except Exception as err:
        log.error("  Error: %s", err)
        return None


async def test_comparison(
    session: aiohttp.ClientSession,
    token: str,
    unit_id: str,
    dump_raw: bool = False,
) -> dict | None:
    """Fetch comparison/overview data."""
    log = logging.getLogger("comparison")

    now = datetime.now()
    end_date = now.strftime("%Y-%m-%d %H:%M")
    start_date = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M")

    url = f"{MYENERGY_USAGE_UNITS_URL}/{unit_id}/compare"
    params = {"startdate": start_date, "enddate": end_date}
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    log.info("GET %s", url)
    try:
        async with session.get(url, params=params, headers=headers) as resp:
            log.info("  Status: %s", resp.status)
            if resp.status == 200:
                data = await resp.json()
                if dump_raw:
                    dump_to_file("comparison", data)
                return data
            else:
                text = await resp.text()
                log.warning("  Failed: %s", text[:500])
                return None
    except Exception as err:
        log.error("  Error: %s", err)
        return None


async def test_comparison_settings(
    session: aiohttp.ClientSession,
    token: str,
    unit_id: str,
    dump_raw: bool = False,
) -> dict | list | None:
    """Fetch comparison settings (available categories)."""
    log = logging.getLogger("comparison_settings")

    url = f"{MYENERGY_USAGE_UNITS_URL}/{unit_id}/comparisonsettings"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    log.info("GET %s", url)
    try:
        async with session.get(url, headers=headers) as resp:
            log.info("  Status: %s", resp.status)
            if resp.status == 200:
                data = await resp.json()
                if dump_raw:
                    dump_to_file("comparison_settings", data)
                return data
            else:
                text = await resp.text()
                log.warning("  Failed: %s", text[:500])
                return None
    except Exception as err:
        log.error("  Error: %s", err)
        return None


async def test_invoice_periods(
    session: aiohttp.ClientSession,
    token: str,
    unit_id: str,
    dump_raw: bool = False,
) -> list | None:
    """Fetch invoice periods."""
    log = logging.getLogger("invoice_periods")

    url = f"{MYENERGY_USAGE_UNITS_URL}/{unit_id}/invoiceperiod"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    log.info("GET %s", url)
    try:
        async with session.get(url, headers=headers) as resp:
            log.info("  Status: %s", resp.status)
            if resp.status == 200:
                data = await resp.json()
                if dump_raw:
                    dump_to_file("invoice_periods", data)
                return data
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

    # Use a shared cookie jar so auth portal cookies carry over
    jar = aiohttp.CookieJar()
    async with aiohttp.ClientSession(cookie_jar=jar) as session:

        # ── Phase 1: Authentication ──────────────────────────────
        print("\n" + "─" * 60)
        print("  PHASE 1: Authentication")
        print("─" * 60)

        token = None

        # Try direct myenergy auth first
        log.info("Trying direct myenergy authentication...")
        token = await test_myenergy_auth(
            session, args.email, args.password, args.dump_raw
        )

        if not token:
            # Try auth portal flow
            log.info("Trying auth portal flow...")
            portal_result = await test_auth_portal(
                session, args.email, args.password, args.dump_raw
            )

            if portal_result["success"]:
                # Check if login response contained a token
                resp = portal_result.get("login_response")
                if isinstance(resp, dict):
                    token = (
                        resp.get("sessionToken")
                        or resp.get("token")
                        or resp.get("accessToken")
                    )

                if not token:
                    # Try myenergy auth again with cookies from portal
                    log.info("Retrying myenergy auth with portal cookies...")
                    token = await test_myenergy_auth(
                        session, args.email, args.password, args.dump_raw
                    )

        if not token:
            print("\n*** AUTHENTICATION FAILED ***")
            print("Could not obtain a Bearer token.")
            print("\nDebug info:")
            print(f"  Cookies in jar: {len(jar)}")
            for cookie in jar:
                print(f"    {cookie.key} = {cookie.value[:30]}... (domain: {cookie.get('domain', 'N/A')})")
            sys.exit(1)

        print(f"\n  Token obtained: {token[:12]}...{token[-4:]}")
        print(f"  Token length: {len(token)}")

        # ── Phase 2: Fetch Usage Units ───────────────────────────
        print("\n" + "─" * 60)
        print("  PHASE 2: Usage Units")
        print("─" * 60)

        units = await test_usage_units(session, token, args.dump_raw)
        if not units:
            print("\n*** No usage units found ***")
            sys.exit(1)

        # Select unit to test
        unit_id = args.unit_id
        if not unit_id:
            # Use the first unit
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

        periods = await test_invoice_periods(
            session, token, unit_id, args.dump_raw
        )
        if periods:
            print_json("Invoice Periods", periods)

        # ── Phase 4: Comparison Settings ─────────────────────────
        print("\n" + "─" * 60)
        print("  PHASE 4: Comparison Settings (Available Categories)")
        print("─" * 60)

        settings = await test_comparison_settings(
            session, token, unit_id, args.dump_raw
        )
        if settings:
            print_json("Comparison Settings", settings)

        # ── Phase 5: Comparison/Overview Data ────────────────────
        print("\n" + "─" * 60)
        print("  PHASE 5: Comparison Overview")
        print("─" * 60)

        comparison = await test_comparison(
            session, token, unit_id, args.dump_raw
        )
        if comparison:
            print_json("Comparison Data", comparison)

        # ── Phase 6: Consumption Data per Category ───────────────
        print("\n" + "─" * 60)
        print("  PHASE 6: Consumption Data (per category)")
        print("─" * 60)

        available_categories = []
        for category in SUPPORTED_CATEGORIES:
            print(f"\n  Testing category: {category}")
            data = await test_consumption(
                session, token, unit_id, category, args.dump_raw
            )
            if data is not None:
                available_categories.append(category)
                print_json(f"Consumption: {category}", data)

        # ── Summary ──────────────────────────────────────────────
        print("\n" + "=" * 60)
        print("  SUMMARY")
        print("=" * 60)
        print(f"  Authentication: OK")
        print(f"  Usage units found: {len(units)}")
        for unit in units:
            uid = unit.get("usageUnitId") or unit.get("id")
            name = unit.get("customName") or unit.get("name") or "unnamed"
            print(f"    - {name} (ID: {uid})")
        print(f"  Invoice periods: {len(periods) if periods else 0}")
        print(f"  Available categories: {available_categories}")
        print(f"  Categories tested: {len(SUPPORTED_CATEGORIES)}")
        print(f"  Categories with data: {len(available_categories)}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
