# NeoVac MyEnergy for Home Assistant

Custom integration for [Home Assistant](https://www.home-assistant.io/) that provides energy and water consumption data from [NeoVac MyEnergy](https://myenergy.neovac.ch) for the **Energy Dashboard**.

## Features

- **Electricity** consumption tracking (kWh)
- **Water** consumption tracking (m³) - total, warm, and cold water
- **Heating** consumption tracking (kWh)
- **Cooling** consumption tracking (kWh)
- Configurable polling interval (default: 15 minutes)
- Compatible with the Home Assistant **Energy Dashboard**
- Automatic discovery of available energy categories per metering point
- UI-based configuration with usage unit (metering point) selection

## Requirements

- A NeoVac MyEnergy account (email + password)
- Home Assistant 2024.1.0 or newer

## Installation

### HACS (Recommended)

1. Open HACS in your Home Assistant instance
2. Go to **Integrations**
3. Click the three dots menu (top right) and select **Custom repositories**
4. Add this repository URL and select **Integration** as the category
5. Click **Add**
6. Search for "NeoVac MyEnergy" and install it
7. Restart Home Assistant

### Manual

1. Copy the `custom_components/neovac` directory to your Home Assistant `custom_components` folder
2. Restart Home Assistant

## Configuration

1. Go to **Settings** > **Devices & Services**
2. Click **+ Add Integration**
3. Search for "NeoVac MyEnergy"
4. Enter your NeoVac MyEnergy email and password
5. Select the usage unit (metering point) you want to monitor
6. The integration will automatically discover available energy categories and create sensors

### Options

After setup, you can configure the polling interval:

1. Go to **Settings** > **Devices & Services**
2. Click on the NeoVac MyEnergy integration
3. Click **Configure**
4. Set the update interval (5-60 minutes, default: 15)

## Energy Dashboard

The sensors created by this integration are fully compatible with the Home Assistant Energy Dashboard:

- **Electricity consumption** sensors use `SensorDeviceClass.ENERGY` with `kWh`
- **Water consumption** sensors use `SensorDeviceClass.WATER` with `m³`
- **Heating/Cooling** sensors use `SensorDeviceClass.ENERGY` with `kWh`

All sensors use `SensorStateClass.TOTAL_INCREASING` for proper long-term statistics tracking.

To add them to the Energy Dashboard:
1. Go to **Settings** > **Dashboards** > **Energy**
2. Add the electricity sensor under **Electricity Grid** > **Grid Consumption**
3. Add water sensors under **Water Consumption**
4. Add heating sensors under **Gas Consumption** (or as individual devices)

## Standalone API Testing

A CLI test script is included to test the API connection without running Home Assistant:

```bash
# Using command-line arguments
python tests/test_api.py --email your@email.com --password yourpassword

# Using environment variables
export NEOVAC_EMAIL=your@email.com
export NEOVAC_PASSWORD=yourpassword
python tests/test_api.py

# With verbose debug output
python tests/test_api.py --email your@email.com --password yourpassword --verbose

# Dump raw API responses to JSON files for debugging
python tests/test_api.py --email your@email.com --password yourpassword --dump-raw

# Query a specific usage unit
python tests/test_api.py --email your@email.com --password yourpassword --unit-id 12345
```

The test script will:
1. Authenticate with the NeoVac API
2. List all available usage units
3. Fetch invoice periods
4. Discover available energy categories
5. Fetch consumption data for each category
6. Print a summary of all findings

## Sensors Created

Depending on your metering setup, the integration creates some or all of these sensors:

| Sensor | Device Class | Unit | Energy Dashboard |
|--------|-------------|------|-----------------|
| Electricity consumption | `energy` | kWh | Grid consumption |
| Water consumption | `water` | m³ | Water consumption |
| Warm water consumption | `water` | m³ | Water consumption |
| Cold water consumption | `water` | m³ | Water consumption |
| Heating consumption | `energy` | kWh | Gas consumption |
| Cooling consumption | `energy` | kWh | Individual device |

## Troubleshooting

### Authentication fails

- Verify your credentials work at [myenergy.neovac.ch](https://myenergy.neovac.ch)
- The integration authenticates via auth.neovac.ch, which is the same login portal used by the web app
- Run the CLI test script with `--verbose` to see detailed authentication debug output

### No sensors created

- Check that your usage unit has active metering for the expected categories
- Run the CLI test script to see which categories are available for your unit
- Check the Home Assistant logs for warnings from the `neovac` integration

### Data not updating

- Check the configured polling interval in the integration options
- The NeoVac API provides data with hourly resolution at best
- New data may take some time to appear in the NeoVac system
