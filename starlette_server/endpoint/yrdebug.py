import argparse
import asyncio
import json
import logging
import sys
from collections import OrderedDict
from pathlib import Path

import pandas as pd

# Assume yrapiclient is importable relative to this script's location
# Adjust the import path if your project structure is different
try:
    from . import yrapiclient
except ImportError:
    # Fallback if running the script directly and yrapiclient is in the same dir
    try:
        import yrapiclient
    except ImportError:
        print("Error: Could not import yrapiclient. Ensure it's installed or in the correct path.", file=sys.stderr)
        sys.exit(1)

from yranalyzer import add_day_night, add_symbol_and_color, create_combined_forecast

# Copied from app_v2.py for standalone debugging
COLORMAPS = OrderedDict()
COLORMAPS["plain"] = {
    "CLEARSKY": [3, 3, 235],
    "PARTLYCLOUDY": [65, 126, 205],
    "CLOUDY": [180, 200, 200],
    "LIGHTRAIN_LT50": [161, 228, 74],
    "LIGHTRAIN": [240, 240, 42],
    "RAIN": [241, 155, 44],
    "HEAVYRAIN": [236, 94, 42],
    "VERYHEAVYRAIN": [234, 57, 248],
}


async def main():
    parser = argparse.ArgumentParser(description="Run YR data analysis for debugging.")
    parser.add_argument("--lat", type=float, required=True, help="Latitude")
    parser.add_argument("--lon", type=float, required=True, help="Longitude")
    parser.add_argument("--nowcast-file", type=Path, help="Path to JSON file with nowcast data (optional)")
    parser.add_argument("--forecast-file", type=Path, help="Path to JSON file with forecast data (optional)")
    parser.add_argument("--slot-minutes", type=int, default=15, help="Time slot length in minutes (default: 15)")
    parser.add_argument("--slot-count", type=int, default=16, help="Number of time slots (default: 16)")
    parser.add_argument(
        "--log",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level (default: INFO)",
    )
    parser.add_argument("--dev", action="store_true", help="Use development mode for API calls (if fetching)")

    args = parser.parse_args()

    # Setup logging
    log_level = getattr(logging, args.log.upper(), logging.INFO)
    # Configure logging to output to stdout
    logging.basicConfig(
        level=log_level, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", stream=sys.stdout
    )
    # Set pandas display options for better DataFrame logging
    pd.set_option("display.max_rows", None)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 1000)

    logger = logging.getLogger("yranalyzer_debug")  # Use a specific logger name
    logger.info("Starting YR Analyzer debug run...")
    logger.info(f"Arguments: {args}")
    colormap = COLORMAPS["plain"]  # Use the built-in colormap
    logger.info("Using built-in 'plain' colormap.")

    # --- Get Nowcast Data ---
    nowcast_data = None
    if args.nowcast_file:
        with open(args.nowcast_file) as f:
            nowcast_data = json.load(f)
        logger.info(f"Loaded nowcast data from file: {args.nowcast_file}")
    else:
        logger.info("Nowcast file not provided. Fetching nowcast data from API...")
        nowcast_data = await yrapiclient.get_nowcast(args.lat, args.lon, args.dev)
        logger.info("Successfully fetched nowcast data from API.")

    # --- Get Forecast Data ---
    forecast_data = None
    if args.forecast_file:
        with open(args.forecast_file) as f:
            forecast_data = json.load(f)
        logger.info(f"Loaded forecast data from file: {args.forecast_file}")
    if forecast_data is None:
        logger.info("Fetching forecast data from API...")
        forecast_data = await yrapiclient.get_locationforecast(args.lat, args.lon, args.dev)
        logger.info("Successfully fetched forecast data from API.")

    # --- Process Data ---
    logger.info("Creating combined forecast...")
    df_combined = create_combined_forecast(nowcast_data, forecast_data, args.slot_minutes, args.slot_count)
    # logger.debug(f"Combined DataFrame created ({len(df_combined)} rows):\n{df_combined.to_string()}")

    logger.info("Adding day/night information...")
    df_daynight = add_day_night(df_combined.copy(), args.lat, args.lon)
    # logger.debug(f"DataFrame with day/night info:\n{df_daynight.to_string()}")

    logger.info("Adding symbol and color information...")
    df_final = add_symbol_and_color(df_daynight.copy(), colormap)
    logger.info(f"Final DataFrame ({len(df_final)} rows):\n{df_final.to_string()}")

    logger.info("YR Analyzer debug run finished successfully.")


asyncio.run(main())
