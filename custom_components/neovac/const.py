"""Constants for the NeoVac MyEnergy integration."""

DOMAIN = "neovac"

# API URLs
AUTH_BASE_URL = "https://auth.neovac.ch"
AUTH_API_URL = f"{AUTH_BASE_URL}/api/v1"
MYENERGY_BASE_URL = "https://myenergy.neovac.ch"
MYENERGY_API_URL = f"{MYENERGY_BASE_URL}/api/v4"

# Auth endpoints (auth.neovac.ch)
AUTH_LOGIN_URL = f"{AUTH_API_URL}/Account/Login"
AUTH_LOGOUT_URL = f"{AUTH_API_URL}/Account/Logout"
AUTH_IS_AUTHENTICATED_URL = f"{AUTH_API_URL}/Account/IsAuthenticated"

# MyEnergy API endpoints (myenergy.neovac.ch)
MYENERGY_AUTH_URL = f"{MYENERGY_API_URL}/account/authenticate"
MYENERGY_ENVIRONMENT_URL = f"{MYENERGY_API_URL}/environment"
MYENERGY_USAGE_UNITS_URL = f"{MYENERGY_API_URL}/usageunits"

# Config entry keys
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_USAGE_UNIT_ID = "usage_unit_id"
CONF_USAGE_UNIT_NAME = "usage_unit_name"
CONF_SCAN_INTERVAL = "scan_interval"

# Defaults
DEFAULT_SCAN_INTERVAL = 15  # minutes
MIN_SCAN_INTERVAL = 5  # minutes
MAX_SCAN_INTERVAL = 60  # minutes

# Energy categories from the NeoVac API
CATEGORY_ELECTRICITY = "Electricity"
CATEGORY_HEATING = "Heating"
CATEGORY_WARM_WATER = "WarmWater"
CATEGORY_WATER = "Water"
CATEGORY_COLD_WATER = "WaterCold"
CATEGORY_COOLING = "Cooling"
CATEGORY_HEAT_PUMP = "HeatPump"
CATEGORY_CHARGING_STATION = "ChargingStation"
CATEGORY_ZEV = "Zev"

# Resolution values for the consumption endpoint
RESOLUTION_HOUR = "Hour"
RESOLUTION_MONTH = "Month"
RESOLUTION_YEAR = "Year"

# Categories we create sensors for
SUPPORTED_CATEGORIES = [
    CATEGORY_ELECTRICITY,
    CATEGORY_WATER,
    CATEGORY_WARM_WATER,
    CATEGORY_COLD_WATER,
    CATEGORY_HEATING,
    CATEGORY_COOLING,
]

PLATFORMS = ["sensor"]
