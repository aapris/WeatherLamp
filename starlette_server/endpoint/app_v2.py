import asyncio
import dataclasses
import datetime
import io
import json
import logging
import math
import os
import re
import traceback
from collections import OrderedDict
from contextlib import asynccontextmanager
from logging.config import dictConfig
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
import sentry_sdk
from pydantic import BaseModel, Field, ValidationError, field_validator
from sentry_sdk.integrations.asgi import SentryAsgiMiddleware
from sentry_sdk.integrations.starlette import StarletteIntegration
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

import yranalyzer
import yrapiclient

# TODO: check handler, perhaps not wsgi?
dictConfig(
    {
        "version": 1,
        "formatters": {"default": {"format": "[%(asctime)s] %(levelname)s in %(module)s: %(message)s"}},
        "handlers": {"wsgi": {"class": "logging.StreamHandler", "formatter": "default"}},
        "root": {"level": os.getenv("LOG_LEVEL", "INFO"), "handlers": ["wsgi"]},
    }
)

# Sentry Initialization
sentry_dsn = os.getenv("SENTRY_DSN")
sentry_enabled = False
if sentry_dsn and sentry_dsn.startswith("https"):
    try:
        sentry_sdk.init(
            dsn=sentry_dsn,
            integrations=[
                StarletteIntegration(
                    transaction_style="endpoint",  # Use route path as transaction name
                )
            ],
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", 0.1)),  # Sample 10% by default
            profiles_sample_rate=float(os.getenv("SENTRY_PROFILES_SAMPLE_RATE", 0.1)),  # Sample 10% by default
            send_default_pii=False,  # Do not send Personally Identifiable Information
        )
        sentry_enabled = True
        logging.info("Sentry initialized successfully.")
    except Exception as e:
        logging.error(f"Failed to initialize Sentry: {e}")

# TODO: these should be in some configuration file
MAX_FORECAST_DURATION_HOURS = 200
SEGMENT_PARTS = 6
COLORMAPS = OrderedDict()

# Stale/error visual indicator thresholds and colors
STALE_WARNING_THRESHOLD_S: int = 30 * 60  # 30 min → last LED colored as warning
ERROR_THRESHOLD_S: int = 3 * 60 * 60  # 3h → full error pattern
STALE_INDICATOR_COLOR: list[int] = [255, 0, 128]  # Hot pink (not in any colormap)
STALE_INDICATOR_HEX: str = "ff0080"
ERROR_PATTERN_COLORS: list[list[int]] = [[255, 0, 128], [0, 0, 0]]  # Alternating hot pink / off


@dataclasses.dataclass
class ForecastResult:
    """Result of a forecast fetch with metadata about data freshness.

    Args:
        df: DataFrame with combined forecast data.
        max_cache_age_seconds: Worst-case staleness across nowcast + forecast sources. None if no cache was involved.
        has_data: False only if forecast source is "none" (total failure).
        data_status: One of "fresh", "stale", or "error".
    """

    df: pd.DataFrame
    max_cache_age_seconds: float | None
    has_data: bool
    data_status: str  # "fresh", "stale", "error"


class RGBColor(BaseModel):
    """RGB color value with validation."""

    red: int = Field(ge=0, le=255, description="Red component (0-255)")
    green: int = Field(ge=0, le=255, description="Green component (0-255)")
    blue: int = Field(ge=0, le=255, description="Blue component (0-255)")

    @field_validator("red", "green", "blue", mode="before")
    @classmethod
    def validate_color_component(cls, v: Any) -> int:
        """Validate that color component is an integer."""
        if not isinstance(v, int):
            raise ValueError(f"Color component must be an integer, got {type(v).__name__}")
        return v

    def to_list(self) -> list[int]:
        """Convert to [R, G, B] list format."""
        return [self.red, self.green, self.blue]

    @classmethod
    def from_list(cls, rgb_list: list[int]) -> "RGBColor":
        """Create RGBColor from [R, G, B] list."""
        if not isinstance(rgb_list, list) or len(rgb_list) != 3:
            raise ValueError("RGB must be a list of exactly 3 integers")
        return cls(red=rgb_list[0], green=rgb_list[1], blue=rgb_list[2])


class WeatherColorMap(BaseModel):
    """Complete colormap definition with validation."""

    CLEARSKY: RGBColor
    PARTLYCLOUDY: RGBColor
    CLOUDY: RGBColor
    LIGHTRAIN_LT50: RGBColor
    LIGHTRAIN: RGBColor
    RAIN: RGBColor
    HEAVYRAIN: RGBColor
    VERYHEAVYRAIN: RGBColor

    @field_validator("*", mode="before")
    @classmethod
    def validate_rgb_field(cls, v: Any) -> RGBColor:
        """Convert list format to RGBColor if needed."""
        if isinstance(v, list):
            return RGBColor.from_list(v)
        elif isinstance(v, RGBColor):
            return v
        else:
            raise ValueError(f"Expected list or RGBColor, got {type(v).__name__}")

    def to_dict(self) -> dict[str, list[int]]:
        """Convert to the legacy dictionary format used by the application."""
        return {field_name: getattr(self, field_name).to_list() for field_name in self.model_fields.keys()}


def load_colormaps() -> None:
    """
    Load all colormap JSON files from the colormaps directory into the global COLORMAPS dictionary.

    Each JSON file should contain color definitions as key-value pairs where keys are weather symbols
    and values are RGB color arrays [R, G, B]. Uses Pydantic for validation.
    """
    colormaps_dir = Path(__file__).parent / "colormaps"

    if not colormaps_dir.exists():
        logging.warning(f"Colormaps directory not found: {colormaps_dir}")
        return

    for json_file in colormaps_dir.glob("*.json"):
        colormap_name = json_file.stem  # filename without extension
        try:
            with open(json_file, encoding="utf-8") as f:
                raw_data = json.load(f)

            # Validate using Pydantic model
            colormap = WeatherColorMap.model_validate(raw_data)

            # Convert to legacy format and store
            COLORMAPS[colormap_name] = colormap.to_dict()
            logging.info(f"Loaded colormap '{colormap_name}' from {json_file}")

        except json.JSONDecodeError as e:
            logging.error(f"Failed to parse JSON in {json_file}: {e}")
        except ValidationError as e:
            logging.error(f"Invalid colormap data in {json_file}: {e}")
        except Exception as e:
            logging.error(f"Failed to load colormap from {json_file}: {e}")

    if not COLORMAPS:
        logging.warning("No valid colormaps loaded, using fallback default")
        # Create fallback using Pydantic model for consistency
        try:
            fallback_data = {
                "CLEARSKY": [3, 3, 235],
                "PARTLYCLOUDY": [65, 126, 205],
                "CLOUDY": [180, 200, 200],
                "LIGHTRAIN_LT50": [161, 228, 74],
                "LIGHTRAIN": [240, 240, 42],
                "RAIN": [241, 155, 44],
                "HEAVYRAIN": [236, 94, 42],
                "VERYHEAVYRAIN": [234, 57, 248],
            }
            fallback_colormap = WeatherColorMap.model_validate(fallback_data)
            COLORMAPS["plain"] = fallback_colormap.to_dict()
        except ValidationError as e:
            logging.critical(f"Failed to create fallback colormap: {e}")
            # This should never happen, but if it does, we need some basic fallback
            COLORMAPS["plain"] = fallback_data


def validate_args_v2(request: Request) -> tuple[str, str, bool, bool, list[dict]]:
    """
    Validate query parameters for V2 endpoint.

    Parses common parameters (format, cm, dev, cm_preview) and segment data from the 's' query parameter.
    The 's' parameter contains one or more segments separated by '+' or ' '.
    Each segment format: index,program,led_count,reversed,lat,lon
    Example: s=1,r5min,12,0,60.167,24.951+2,r15min,8,1,60.167,24.951

    Common parameters:
    - format: response format (json_wled, json, html, bin)
    - cm: colormap name (defaults to "plain")
    - dev: use development/sample data instead of live API
    - cm_preview: return colormap colors scaled to segment length instead of weather data
    - s: segment definitions

    :param request: starlette.requests.Request
    :return: tuple containing response_format, colormap, dev flag, cm_preview flag, and a list of segment dictionaries.
             Each segment dictionary contains: index, program, led_count, reversed, lat, lon, slot_minutes.
    :raises HTTPException: if parameters are invalid or missing.
    """
    response_format = request.query_params.get("format", "json_wled")
    colormap = request.query_params.get("cm", "plain")
    dev = request.query_params.get("dev") is not None
    cm_preview = request.query_params.get("cm_preview") is not None

    s_param = request.query_params.get("s")
    if not s_param:
        error_detail = {
            "error_code": "MISSING_S_QUERY_PARAM",
            "message": "Missing 's' query parameter",
        }
        raise HTTPException(status_code=400, detail=error_detail)

    segments_data = []
    segments = s_param.split(" ")

    for segment_str in segments:
        parts = segment_str.split(",")
        if len(parts) != SEGMENT_PARTS:
            error_detail = {
                "error_code": "INVALID_SEGMENT_FORMAT",
                "message": "Invalid segment format. Expected 6 comma-separated values.",
                "details": {"segment": segment_str},
            }
            raise HTTPException(status_code=400, detail=error_detail)

        try:
            index = int(parts[0])
            program = parts[1]
            led_count = int(parts[2])  # Corresponds to old 'slot_count'
            reversed_flag = int(parts[3])
            lat = round(float(parts[4]), 3)
            lon = round(float(parts[5]), 3)

            if reversed_flag not in [0, 1]:
                raise ValueError("Reversed flag must be 0 or 1")

            # Handle "dark" program type
            if program == "dark":
                slot_minutes = 0  # Not needed for dark segments
            else:
                # Derive slot_minutes from program string (e.g., "r5min", "program15min")
                match = re.search(r"(\d+)min$", program)  # Use search and match digits followed by 'min' at the end
                if not match:
                    # Handle other potential program types or raise error
                    raise ValueError(
                        f"Invalid program format: '{program}'. Expected format ending like '5min', '15min' etc., or 'dark'."
                    )
                slot_minutes = int(match.group(1))

                # Validate total duration (equivalent to old check)
                # led_count is used as slot_count here
                if slot_minutes / 60 * led_count > MAX_FORECAST_DURATION_HOURS:
                    error_detail = {
                        "error_code": "DURATION_TOO_LONG",
                        "message": f"Derived interval * led_count cannot exceed {MAX_FORECAST_DURATION_HOURS} hours for a segment.",
                        "details": {"segment": segment_str, "derived_duration_hours": slot_minutes / 60 * led_count},
                    }
                    raise HTTPException(status_code=400, detail=error_detail)

            segment_info = {
                "index": index,
                "program": program,
                "led_count": led_count,
                "reversed": reversed_flag,
                "lat": lat,
                "lon": lon,
                "slot_minutes": slot_minutes,
            }
            segments_data.append(segment_info)

        except (ValueError, TypeError) as e:
            error_detail = {
                "error_code": "INVALID_SEGMENT_DATA",
                "message": f"Invalid data in segment: {e}",
                "details": {"segment": segment_str},
            }
            raise HTTPException(status_code=400, detail=error_detail) from e
        except IndexError:
            # This case might be less likely now due to the length check above,
            # but kept for robustness.
            error_detail = {
                "error_code": "INVALID_SEGMENT_FORMAT",
                "message": "Invalid segment format. Error accessing parts.",
                "details": {"segment": segment_str},
            }
            raise HTTPException(status_code=400, detail=error_detail) from None

    return response_format, colormap, dev, cm_preview, segments_data


async def create_forecast(
    lat: float, lon: float, slot_minutes: int, slot_count: int, dev: bool = False
) -> ForecastResult:
    """
    Request YR nowcast and forecast from cache or API and create single DataFrame from the data.

    Returns a ForecastResult containing the DataFrame and metadata about data freshness,
    enabling the caller to apply stale/error visual indicators.

    :param lat: latitude
    :param lon: longitude
    :param slot_minutes: time slot duration in minutes
    :param slot_count: number of time slots
    :param dev: use local sample response data instead of remote API
    :return: ForecastResult with DataFrame and freshness metadata
    """
    nowcast_result, forecast_result = await asyncio.gather(
        yrapiclient.get_nowcast(lat, lon, dev),
        yrapiclient.get_locationforecast(lat, lon, dev),
    )

    # Determine worst-case cache age across both sources
    ages = [r.cache_age_seconds for r in (nowcast_result, forecast_result) if r.cache_age_seconds is not None]
    max_cache_age = max(ages) if ages else None

    # has_data is False only when forecast completely failed (nowcast alone isn't enough)
    has_data = forecast_result.source != "none"

    # Determine data status
    if not has_data:
        data_status = "error"
    elif max_cache_age is not None and max_cache_age > STALE_WARNING_THRESHOLD_S:
        data_status = "stale"
    else:
        data_status = "fresh"

    df = yranalyzer.create_combined_forecast(nowcast_result.data, forecast_result.data, slot_minutes, slot_count)

    if len(df.index) != slot_count:
        logging.warning(f"Expected {slot_count} slots, but got {len(df.index)} for {lat},{lon}. Padding/truncating.")
        if len(df.index) < slot_count:
            last_row = df.iloc[[-1]] if not df.empty else None
            missing_rows = slot_count - len(df.index)
            if last_row is not None:
                padding_df = pd.concat([last_row] * missing_rows, ignore_index=True)
                df = pd.concat([df, padding_df], ignore_index=True)
        else:
            df = df.iloc[:slot_count]

    return ForecastResult(
        df=df,
        max_cache_age_seconds=max_cache_age,
        has_data=has_data,
        data_status=data_status,
    )


async def create_forecast_df(lat: float, lon: float, dev: bool = False) -> pd.DataFrame:
    """
    Request YR nowcast and forecast from cache or API and create single DataFrame from the data.

    :param lat: latitude
    :param lon: longitude
    :param dev: use local sample response data instead of remote API
    :return: DataFrame with precipitation forecast
    """
    nowcast_result, forecast_result = await asyncio.gather(
        yrapiclient.get_nowcast(lat, lon, dev),
        yrapiclient.get_locationforecast(lat, lon, dev),
    )
    df = yranalyzer.create_combined_forecast(nowcast_result.data, forecast_result.data)
    return df


def _build_error_pattern(led_count: int) -> list[dict[str, Any]]:
    """Build an alternating hot pink / off error pattern for the entire segment.

    Args:
        led_count: Number of LEDs in the segment.

    Returns:
        List of LED slot dictionaries with alternating error colors.
    """
    times = []
    for idx in range(led_count):
        color = ERROR_PATTERN_COLORS[idx % len(ERROR_PATTERN_COLORS)]
        hex_color = f"{color[0]:02x}{color[1]:02x}{color[2]:02x}"
        times.append(
            {
                "time": None,
                "yr_symbol": None,
                "wl_symbol": "error",
                "prec_nowcast": None,
                "prec_forecast": None,
                "precipitation": None,
                "prob_of_prec": None,
                "wind_gust": None,
                "rgb": color,
                "hex": hex_color,
            }
        )
    return times


async def _get_segment_data(
    lat: float, lon: float, slot_minutes: int, slot_count: int, colormap_name: str, dev: bool = False
) -> tuple[list[dict[str, Any]], str]:
    """
    Fetches and processes weather forecast data for a single segment.

    Returns the LED data list and a data_status string ("fresh", "stale", "error").

    :param lat: latitude
    :param lon: longitude
    :param slot_minutes: minutes for pandas resample function
    :param slot_count: how many slots will be returned
    :param colormap_name: pre-defined color map name
    :param dev: use local sample response data instead of remote API
    :return: Tuple of (LED data list, data_status string).
    """
    result = await create_forecast(lat, lon, slot_minutes, slot_count, dev)
    df = result.df

    # No data or data too old → full error pattern
    if not result.has_data or (
        result.max_cache_age_seconds is not None and result.max_cache_age_seconds > ERROR_THRESHOLD_S
    ):
        logging.warning(
            f"Error condition for {lat},{lon}: has_data={result.has_data}, "
            f"max_age={result.max_cache_age_seconds}s. Returning error pattern."
        )
        return _build_error_pattern(slot_count), "error"

    times = []

    if colormap_name in COLORMAPS:
        colormap = COLORMAPS[colormap_name]
    else:
        first_colormap_name = next(iter(COLORMAPS.keys()))
        colormap = COLORMAPS[first_colormap_name]
        logging.warning(f"Colormap '{colormap_name}' not found, using default '{first_colormap_name}'.")

    logging.debug(f"DataFrame for segment {lat},{lon}:\n{df.to_string()}")
    df = yranalyzer.add_symbol_and_color(df, colormap)
    df = yranalyzer.add_day_night(df, lat, lon)
    logging.info(f"Processed DataFrame for segment {lat},{lon}:\n{df.to_string()}")

    for i in df.index:
        if i not in df.index:
            logging.warning(f"Index {i} not found in DataFrame for {lat},{lon} after processing. Skipping.")
            continue

        color_data = df.get("color", [0, 0, 0])[i]
        if isinstance(color_data, list) and len(color_data) == 3:
            hex_color = f"{color_data[0]:02x}{color_data[1]:02x}{color_data[2]:02x}"
            rgb_color = color_data
        else:
            logging.warning(
                f"Invalid or missing color data for index {i} at {lat},{lon}. Using default [0,0,0]. Data: {color_data}"
            )
            hex_color = "000000"
            rgb_color = [0, 0, 0]

        yr_symbol = df.get("symbol", None)[i] if "symbol" in df.columns else None
        wl_symbol = df.get("wl_symbol", None)[i] if "wl_symbol" in df.columns else None
        prec_now = df.get("prec_now", None)[i] if "prec_now" in df.columns else None
        prec_fore = df.get("prec_fore", None)[i] if "prec_fore" in df.columns else None
        prob_of_prec = df.get("prob_of_prec", None)[i] if "prob_of_prec" in df.columns else None
        wind_gust = df.get("wind_gust", None)[i] if "wind_gust" in df.columns else None

        precipitation = prec_now if pd.notnull(prec_now) else prec_fore

        times.append(
            {
                "time": str(df.index[df.index.get_loc(i)]),
                "yr_symbol": yr_symbol,
                "wl_symbol": wl_symbol,
                "prec_nowcast": prec_now,
                "prec_forecast": prec_fore,
                "precipitation": precipitation,
                "prob_of_prec": prob_of_prec,
                "wind_gust": wind_gust,
                "rgb": rgb_color,
                "hex": hex_color,
            }
        )

    # Apply stale indicator: set last LED to hot pink if data is stale (before reversal)
    if result.data_status == "stale" and times:
        times[-1]["rgb"] = STALE_INDICATOR_COLOR
        times[-1]["hex"] = STALE_INDICATOR_HEX
        times[-1]["wl_symbol"] = "stale_indicator"

    return times, result.data_status


def _create_colormap_preview(colormap_name: str, led_count: int) -> list[dict[str, Any]]:
    """
    Creates a preview of the colormap by distributing all colors evenly across the segment length.

    :param colormap_name: Name of the colormap to use
    :param led_count: Number of LEDs in the segment
    :return: List of dictionaries containing color data for each LED position
    """
    if colormap_name in COLORMAPS:
        colormap = COLORMAPS[colormap_name]
    else:
        # Get the first colormap as default if the requested one doesn't exist
        first_colormap_name = next(iter(COLORMAPS.keys()))
        colormap = COLORMAPS[first_colormap_name]
        logging.warning(f"Colormap '{colormap_name}' not found, using default '{first_colormap_name}'.")

    # Get all colors from the colormap
    colors = list(colormap.values())  # This gives us all RGB color arrays

    # Create preview data by cycling through colors
    preview_data = []
    for i in range(led_count):
        # Distribute colors evenly across the segment
        color_index = int((i / led_count) * len(colors)) if led_count > 1 else 0
        # Ensure we don't exceed the color list bounds
        if color_index >= len(colors):
            color_index = len(colors) - 1

        rgb_color = colors[color_index]
        hex_color = f"{rgb_color[0]:02x}{rgb_color[1]:02x}{rgb_color[2]:02x}"

        preview_data.append(
            {
                "time": None,
                "yr_symbol": None,
                "wl_symbol": f"colormap_preview_{list(colormap.keys())[color_index]}",
                "prec_nowcast": None,
                "prec_forecast": None,
                "precipitation": None,
                "prob_of_prec": None,
                "wind_gust": None,
                "rgb": rgb_color,
                "hex": hex_color,
            }
        )

    return preview_data


async def _process_segments(
    segments_data: list[dict[str, Any]], colormap: str, dev: bool, cm_preview: bool = False
) -> tuple[list[list[dict[str, Any]]], list[str]]:
    """
    Processes forecast data for all requested segments.

    Synchronous segments (dark, cm_preview) are resolved immediately.
    Weather segments are fetched concurrently via asyncio.gather().

    :param segments_data: List of segment definitions from validation.
    :param colormap: Name of the colormap to use.
    :param dev: Flag to use development data.
    :param cm_preview: Flag to return colormap preview instead of weather data.
    :return: Tuple of (segment data lists, per-segment data_status strings).
    :raises Exception: Propagates exceptions from underlying calls like _get_segment_data.
    """
    # Pre-allocate result lists
    output_segments_data: list[list[dict[str, Any]] | None] = [None] * len(segments_data)
    segment_statuses: list[str] = ["fresh"] * len(segments_data)

    # Collect async tasks and their indices
    async_tasks = []
    async_indices = []

    for i, segment in enumerate(segments_data):
        logging.debug(f"Processing segment: {segment}")

        # Handle colormap preview mode (synchronous)
        if cm_preview:
            segment_times = _create_colormap_preview(colormap, segment["led_count"])
            if segment["reversed"]:
                segment_times = segment_times[::-1]
            output_segments_data[i] = segment_times
            logging.debug(f"Generated colormap preview for segment index {segment['index']}")
            continue

        # Handle "dark" segments directly (synchronous)
        if segment["program"] == "dark":
            dark_segment_times = []
            for _ in range(segment["led_count"]):
                dark_segment_times.append(
                    {
                        "time": None,
                        "yr_symbol": None,
                        "wl_symbol": "dark",
                        "prec_nowcast": None,
                        "prec_forecast": None,
                        "precipitation": None,
                        "prob_of_prec": None,
                        "wind_gust": None,
                        "rgb": [0, 0, 0],
                        "hex": "000000",
                    }
                )
            output_segments_data[i] = dark_segment_times
            logging.debug(f"Generated dark segment data for segment index {segment['index']}")
            continue

        # Queue weather segments for concurrent processing
        async_tasks.append(
            _get_segment_data(
                lat=segment["lat"],
                lon=segment["lon"],
                slot_minutes=segment["slot_minutes"],
                slot_count=segment["led_count"],
                colormap_name=colormap,
                dev=dev,
            )
        )
        async_indices.append(i)

    # Fetch all weather segments concurrently
    if async_tasks:
        try:
            results = await asyncio.gather(*async_tasks)
        except Exception as e:
            logging.exception(f"Error processing weather segments: {e}")
            raise

        for idx, (segment_times, data_status) in zip(async_indices, results, strict=True):
            if segments_data[idx]["reversed"]:
                segment_times = list(reversed(segment_times))
            output_segments_data[idx] = segment_times
            segment_statuses[idx] = data_status

    return output_segments_data, segment_statuses


def _format_html(processed_data: list[list[dict[str, Any]]]) -> str:
    """Formats the processed segment data into an HTML table."""
    html_parts = [
        """<html><head>
    <title>WeatherLamp V2 Output</title>
    <style>
      body { margin: 10px; font-family: sans-serif; }
      table { border-collapse: collapse; margin-bottom: 20px; }
      th, td { border: 1px solid #ccc; padding: 5px 8px; text-align: left; }
      th { background-color: #f0f0f0; }
      .segment-header { margin-top: 20px; font-size: 1.2em; font-weight: bold; }
    </style>
    </head><body><h1>WeatherLamp V2 Output</h1>"""
    ]

    for _, segment_data in enumerate(processed_data):
        # Add segment header (using index or coordinates)
        # Assuming segment data might be empty, handle gracefully
        # coords = (
        #     f"Lat: {segment_data[0]['lat']}, Lon: {segment_data[0]['lon']}" if segment_data else f"Segment {i + 1}"
        # )
        # # If we have access to original segments_data, we could use index/program etc.
        # html_parts.append(f"<div class='segment-header'>Segment {i + 1} ({coords})</div>")  # Simple header for now
        html_parts.append("<table>\n<tr>")
        # Use keys from the first item as headers (ensure data exists)
        if segment_data:
            # headers = [  # Old hardcoded headers
            #     "time",
            #     "yr_symbol",
            #     "wl_symbol",
            #     "prec_nowcast",
            #     "prec_forecast",
            #     "precipitation",
            #     "prob_of_prec",
            #     "wind_gust",
            #     "hex",
            # ]
            headers = list(segment_data[0].keys())  # Use keys from the first data item
            html_parts.append("<tr>")  # Add row opening tag here
            for header in headers:
                html_parts.append(f"<th>{header}</th>")
            html_parts.append("</tr>\n")
            for t in segment_data:
                formatted_color = f"rgb({','.join(map(str, t.get('rgb', [0, 0, 0])))})"  # Default color if missing
                html_parts.append(f"<tr style='background-color: {formatted_color}'>")
                for header in headers:
                    # Handle potential missing keys gracefully
                    value = t.get(header, "N/A")
                    # Format floats for better readability if needed
                    if isinstance(value, float):
                        value = f"{value:.2f}" if pd.notnull(value) else "N/A"
                    html_parts.append(f"<td>{value}</td>")
                html_parts.append("</tr>")
        else:
            html_parts.append("<tr><td>No data for this segment.</td></tr>")

        html_parts.append("</table>")

    html_parts.append("</body></html>")
    return "\n".join(html_parts)


def _replace_nan_with_none(obj: Any, nan_found: list[bool]) -> Any:
    """Recursively replace float NaN with None in a nested structure.

    Also updates the nan_found flag if a NaN is encountered.
    """
    if isinstance(obj, dict):
        return {k: _replace_nan_with_none(v, nan_found) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_replace_nan_with_none(elem, nan_found) for elem in obj]
    elif isinstance(obj, float) and math.isnan(obj):
        nan_found[0] = True  # Set the flag
        return None
    else:
        return obj


def _format_json(processed_data: list[list[dict[str, Any]]], segment_statuses: list[str] | None = None) -> str:
    """Formats the processed segment data into a standard JSON string, replacing NaN with null.

    When segment_statuses is provided, wraps each segment with metadata including data_status.
    """
    nan_found_tracker = [False]
    data_without_nan = _replace_nan_with_none(processed_data, nan_found_tracker)

    if nan_found_tracker[0]:
        logging.debug("NaN values found and replaced with null in JSON output.")

    if segment_statuses:
        # Wrap segments with metadata
        output = []
        for i, segment_data in enumerate(data_without_nan):
            status = segment_statuses[i] if i < len(segment_statuses) else "fresh"
            output.append({"data_status": status, "data": segment_data})
        return json.dumps(output)

    return json.dumps(data_without_nan)


def _format_json_wled(processed_data: list[list[dict[str, Any]]]) -> str:
    """Formats the processed segment data into WLED-compatible JSON."""
    wled_output = []
    for segment_data in processed_data:
        wled_segment = [{"hex": item.get("hex", "000000")} for item in segment_data]  # Default hex if missing
        wled_output.append(wled_segment)
    # Use compact separators for WLED
    return json.dumps(wled_output, separators=(",", ":"))


def _format_bin(processed_data: list[list[dict[str, Any]]]) -> bytes:
    """Formats the processed segment data into a binary byte array (concatenated RGB values)."""
    all_bytes = bytearray()
    for segment_data in processed_data:
        for item in segment_data:
            rgb = item.get("rgb", [0, 0, 0])  # Default color if missing
            # Ensure RGB is a list of 3 integers
            if isinstance(rgb, list) and len(rgb) == 3 and all(isinstance(c, int) for c in rgb):
                all_bytes.extend(bytes(rgb))
            else:
                logging.warning(f"Invalid RGB data found in segment for binary output: {rgb}. Using [0,0,0].")
                all_bytes.extend(bytes([0, 0, 0]))  # Append black for invalid data

    return bytes(all_bytes)


async def v2(request: Request) -> Response:
    """
    Get rain forecast from YR API and return response in the requested format (html, json, json_wled, bin).

    Handles validation and processing exceptions internally, raising HTTPException for client errors
    and allowing other exceptions to propagate for centralized handling.

    :param request: starlette.requests.Request
    :return: Response
    :raises HTTPException: For validation errors or unsupported formats.
    :raises Exception: For internal processing errors during segment data fetching/processing.
    """
    try:
        response_format, colormap, dev, cm_preview, segments_data = validate_args_v2(request)
        logging.info(
            f"Request validated: format={response_format}, colormap={colormap}, dev={dev}, cm_preview={cm_preview}, segments={len(segments_data)}"
        )
    except HTTPException as e:
        # Validation errors are specific client errors, log and re-raise
        logging.warning(f"Validation failed: {e.detail}")
        raise e  # Caught by http_exception_handler
    processed_segments_data, segment_statuses = await _process_segments(segments_data, colormap, dev, cm_preview)
    logging.debug(f"Processed data for {len(processed_segments_data)} segments.")

    # Format the data based on the requested format
    if response_format == "html":
        content = _format_html(processed_segments_data)
        return HTMLResponse(content)
    elif response_format == "json":
        content = _format_json(processed_segments_data, segment_statuses)
        return Response(content, media_type="application/json")
    elif response_format == "json_wled":
        content = _format_json_wled(processed_segments_data)
        return Response(content, media_type="application/json")
    elif response_format == "bin":
        content_bytes = _format_bin(processed_segments_data)
        return StreamingResponse(io.BytesIO(content_bytes), media_type="application/octet-stream")
    else:
        # This case should be caught by validation, but handle defensively
        logging.error(f"Unsupported response format requested: {response_format}")
        raise HTTPException(
            status_code=400,
            detail={"error_code": "INVALID_FORMAT", "message": f"Unsupported format: {response_format}"},
        )


path = os.getenv("ENDPOINT_PATH", "/v2")

# Path to the UI HTML file, relative to this module's location
HTML_FILE_PATH = Path(__file__).parent / "web" / "index.html"


async def serve_ui(request: Request) -> Response:
    """Serves the index.html file for the UI."""
    return FileResponse(HTML_FILE_PATH)


async def health_check(request: Request) -> Response:
    """Health check endpoint for load balancer and deployment automation."""
    return JSONResponse({"status": "healthy", "timestamp": datetime.datetime.now(datetime.UTC).isoformat()})


routes = [
    Route(path, endpoint=v2, methods=["GET", "POST", "HEAD"]),
    Route(path + "/ui", endpoint=serve_ui, methods=["GET"]),  # Add UI route
    Route(path + "/health", endpoint=health_check, methods=["GET"]),  # Health check endpoint
]

debug = True if os.getenv("DEBUG") else False


@asynccontextmanager
async def lifespan(app: Starlette):
    """Manage shared resources across the application lifecycle.

    Creates data directories, then creates and closes a shared httpx.AsyncClient.
    Creating directories here (rather than on each request) keeps the request path
    free of blocking filesystem calls.

    Args:
        app: The Starlette application instance.
    """
    error_dump_dir = Path(os.getenv("ERROR_DUMP_DIR", str(yrapiclient.DATA_DIR / "error_dumps")))
    for data_dir in (
        yrapiclient.DATA_DIR / "cache",
        yrapiclient.DATA_DIR / "history",
        error_dump_dir,
    ):
        data_dir.mkdir(parents=True, exist_ok=True)

    yrapiclient.http_client = httpx.AsyncClient(
        headers={"User-Agent": yrapiclient.USER_AGENT},
        timeout=httpx.Timeout(10.0),
    )
    yield
    await yrapiclient.http_client.aclose()
    yrapiclient.http_client = None


app = Starlette(debug=debug, routes=routes, lifespan=lifespan)


# --- Error Dumping Function ---
def _write_error_dump(filepath: Path, timestamp: str, exc: Exception, request_info: dict) -> None:
    """Write error details to a file synchronously.

    Args:
        filepath: Path to the error dump file.
        timestamp: Formatted timestamp string.
        exc: The exception that occurred.
        request_info: Dictionary with request details (url, method, client_host, body).
    """
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(f"Timestamp: {timestamp}\n")
        f.write(f"Exception Type: {type(exc).__name__}\n")
        f.write(f"Exception Message: {exc}\n\n")

        f.write("--- Request Details ---\n")
        if request_info:
            f.write(f"URL: {request_info.get('url', 'N/A')}\n")
            f.write(f"Method: {request_info.get('method', 'N/A')}\n")
            f.write(f"Client Host: {request_info.get('client_host', 'N/A')}\n")
            body = request_info.get("body")
            if body is not None:
                max_body_log_size = 1024 * 10
                f.write("Body:\n")
                if len(body) > max_body_log_size:
                    f.write(f"(Truncated, size={len(body)} > {max_body_log_size})\n")
                    f.write(body[:max_body_log_size].decode(errors="replace"))
                    f.write("...\n")
                else:
                    f.write(body.decode(errors="replace"))
                    f.write("\n")
        else:
            f.write("Request object not available.\n")

        f.write("\n--- Traceback ---\n")
        traceback.print_exc(file=f)


async def dump_error_to_file(request: Request | None, exc: Exception):
    """Dumps detailed error information to a timestamped file."""
    logging.info("Entered dump_error_to_file")
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    dump_dir = Path(os.getenv("ERROR_DUMP_DIR", str(yrapiclient.DATA_DIR / "error_dumps")))
    filename = f"error_dump_{timestamp}.log"
    try:
        dump_dir.mkdir(parents=True, exist_ok=True)
        filepath = dump_dir / filename

        # Gather request info before offloading to thread
        request_info = {}
        if request:
            request_info["url"] = str(request.url)
            request_info["method"] = request.method
            request_info["client_host"] = request.client.host if request.client else "N/A"
            try:
                request_info["body"] = await request.body()
            except Exception as body_exc:
                request_info["body_error"] = str(body_exc)

        await asyncio.to_thread(_write_error_dump, filepath, timestamp, exc, request_info)
        logging.info(f"Error details dumped to {filepath}")
    except Exception as dump_exc:
        logging.error(f"CRITICAL: Failed to dump error details to {filename}. Dump Error: {dump_exc}", exc_info=True)
        logging.error(f"Original Exception ({type(exc).__name__}): {exc}", exc_info=False)
    logging.info("Exiting dump_error_to_file")


# --- Custom Exception Handlers ---
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """
    Custom exception handler for HTTPException. Dumps error and returns JSON response.
    """
    # Dump the error details first
    await dump_error_to_file(request, exc)

    # Prepare JSON response content
    if isinstance(exc.detail, dict):
        error_content = {
            "error_code": exc.detail.get("error_code", f"HTTP_{exc.status_code}"),
            "message": exc.detail.get("message", "An error occurred."),
        }
        if "details" in exc.detail:
            error_content["details"] = exc.detail["details"]
    else:
        error_content = {"error_code": f"HTTP_{exc.status_code}", "message": str(exc.detail)}

    # Log the handled HTTP exception (less severe than unhandled ones)
    logging.warning(
        f"Handled HTTPException: Status={exc.status_code}, Code={error_content.get('error_code')}, Msg={error_content.get('message')}"
        # Avoid logging full exc_info for common HTTPExceptions unless needed
        # exc_info=exc if debug else False
    )

    return JSONResponse(status_code=exc.status_code, content=error_content)


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Custom handler for unhandled Exceptions. Dumps error and returns a generic 500 JSON response.
    """
    logging.info(f"Entered generic_exception_handler for exception: {type(exc).__name__}")
    # Log the critical unhandled exception with stack trace
    logging.exception(f"Unhandled exception during request to {request.url}: {exc}", exc_info=exc)

    logging.info("Attempting to dump error to file...")
    # Dump the error details to file
    try:
        await dump_error_to_file(request, exc)
        logging.info("Successfully called dump_error_to_file.")
    except Exception as dump_call_exc:
        logging.error(f"Exception occurred during call to dump_error_to_file: {dump_call_exc}", exc_info=True)

    # Explicitly capture exception in Sentry and attempt to flush
    if sentry_enabled:
        logging.info("Attempting to capture exception to Sentry and flush...")
        try:
            sentry_sdk.capture_exception(exc)
            await asyncio.to_thread(sentry_sdk.flush, 5.0)  # Give 5 seconds for flush
            logging.info("Sentry capture_exception and flush called.")
        except Exception as sentry_exc:
            logging.error(f"Exception occurred during Sentry capture/flush: {sentry_exc}", exc_info=True)

    logging.info("Returning 500 JSON response from generic_exception_handler.")
    # Return a generic 500 error response
    return JSONResponse(
        status_code=500,
        content={
            "error_code": "INTERNAL_SERVER_ERROR",
            "message": "An unexpected internal server error occurred.",
            # Optionally include details in debug mode, but be cautious
            "details": str(exc) if debug else None,
        },
    )


# Load colormaps from files
load_colormaps()
logging.info(f"Loaded {len(COLORMAPS)} colormaps: {list(COLORMAPS.keys())}")

# Register the custom exception handlers BEFORE wrapping with Sentry
app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(Exception, generic_exception_handler)  # Catch all other exceptions


# Wrap with Sentry middleware if enabled
if sentry_enabled:
    try:
        app = SentryAsgiMiddleware(app)
        logging.info("Sentry ASGI middleware enabled.")
    except Exception as e:
        logging.error(f"Failed to wrap app with Sentry middleware: {e}")

logging.info(f"Sentry enabled: {sentry_enabled}, Sentry DSN: {sentry_dsn}")
