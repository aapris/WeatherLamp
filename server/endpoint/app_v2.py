import datetime
import io
import json
import logging
import math
import os
import re
import traceback
from collections import OrderedDict
from logging.config import dictConfig
from pathlib import Path
from typing import Any

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
MAX_FORECAST_DURATION_HOURS = 48
SEGMENT_PARTS = 6
COLORMAPS = OrderedDict()


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
) -> pd.DataFrame:
    """
    Request YR nowcast and forecast from cache or API and create single DataFrame from the data.

    :param lat: latitude
    :param lon: longitude
    :param slot_minutes:
    :param slot_count:
    :param dev: use local sample response data instead of remote API
    :return: DataFrame with precipitation forecast for next slot_count of slot_minutes 16
    """
    nowcast = await yrapiclient.get_nowcast(lat, lon, dev)
    forecast = await yrapiclient.get_locationforecast(lat, lon, dev)
    df = yranalyzer.create_combined_forecast(nowcast, forecast, slot_minutes, slot_count)
    # TODO: append missing rows instead of raising exception
    if len(df.index) != slot_count:
        logging.warning(f"Expected {slot_count} slots, but got {len(df.index)} for {lat},{lon}. Padding/truncating.")
        # Simple padding/truncating logic:
        if len(df.index) < slot_count:
            # Pad with last row if needed (or create default rows)
            last_row = df.iloc[[-1]] if not df.empty else None  # Handle empty DataFrame
            missing_rows = slot_count - len(df.index)
            if last_row is not None:
                padding_df = pd.concat([last_row] * missing_rows, ignore_index=True)
                # Adjust index if necessary, or let concat handle it if appropriate
                df = pd.concat([df, padding_df], ignore_index=True)
            # TODO: Consider a more sophisticated padding strategy if needed
        else:
            # Truncate if too long
            df = df.iloc[:slot_count]

    # assert len(df.index) == slot_count # Removed assert
    return df


async def create_forecast_df(lat: float, lon: float, dev: bool = False) -> pd.DataFrame:
    """
    Request YR nowcast and forecast from cache or API and create single DataFrame from the data.

    :param lat: latitude
    :param lon: longitude
    :param dev: use local sample response data instead of remote API
    :return: DataFrame with precipitation forecast for next slot_count of slot_minutes 16
    """
    nowcast = await yrapiclient.get_nowcast(lat, lon, dev)
    forecast = await yrapiclient.get_locationforecast(lat, lon, dev)
    df = yranalyzer.create_combined_forecast(nowcast, forecast)
    return df


async def _get_segment_data(
    lat: float, lon: float, slot_minutes: int, slot_count: int, colormap_name: str, dev: bool = False
) -> list[dict[str, Any]]:
    """
    Fetches and processes weather forecast data for a single segment.

    :param lat: latitude
    :param lon: longitude
    :param slot_minutes: minutes for pandas resample function
    :param slot_count: how many slots will be returned
    :param colormap_name: pre-defined color map name
    :param dev: use local sample response data instead of remote API
    :return: List of dictionaries, each representing a time slot's data.
    """
    df = await create_forecast(lat, lon, slot_minutes, slot_count, dev)
    times = []

    if colormap_name in COLORMAPS:
        colormap = COLORMAPS[colormap_name]
    else:
        # Get the first colormap as default if the requested one doesn't exist
        first_colormap_name = next(iter(COLORMAPS.keys()))
        colormap = COLORMAPS[first_colormap_name]
        logging.warning(f"Colormap '{colormap_name}' not found, using default '{first_colormap_name}'.")
    print("df", df)
    df = yranalyzer.add_symbol_and_color(df, colormap)
    df = yranalyzer.add_day_night(df, lat, lon)
    # Ensure display options don't affect data processing
    # pd.set_option("display.max_rows", None, "display.max_columns", None, "display.width", 1000) # Not needed for processing
    logging.info(f"Processed DataFrame for segment {lat},{lon}:\n{df.to_string()}")

    for i in df.index:
        # This handles potential missing indices if create_forecast padding failed or was complex
        if i not in df.index:
            logging.warning(f"Index {i} not found in DataFrame for {lat},{lon} after processing. Skipping.")
            continue
        # Take always nowcast's precipitation if available, otherwise forecast's
        precipitation = df.loc[i, "prec_now"] if pd.notnull(df.loc[i, "prec_now"]) else df.loc[i, "prec_fore"]

        # Ensure color exists before accessing elements
        # Use .get() with a default for color as well, in case the column is missing entirely
        color_data = df.get("color", [0, 0, 0])[i]  # Access the value at index i
        if isinstance(color_data, list) and len(color_data) == 3:
            hex_color = f"{color_data[0]:02x}{color_data[1]:02x}{color_data[2]:02x}"
            rgb_color = color_data
        else:
            logging.warning(
                f"Invalid or missing color data for index {i} at {lat},{lon}. Using default [0,0,0]. Data: {color_data}"
            )
            hex_color = "000000"
            rgb_color = [0, 0, 0]  # Default to black

        # Use .get(column, None) to safely access potentially missing data
        yr_symbol = df.get("symbol", None)[i] if "symbol" in df.columns else None
        wl_symbol = df.get("wl_symbol", None)[i] if "wl_symbol" in df.columns else None
        prec_now = df.get("prec_now", None)[i] if "prec_now" in df.columns else None
        prec_fore = df.get("prec_fore", None)[i] if "prec_fore" in df.columns else None
        prob_of_prec = df.get("prob_of_prec", None)[i] if "prob_of_prec" in df.columns else None
        wind_gust = df.get("wind_gust", None)[i] if "wind_gust" in df.columns else None

        # Recalculate precipitation based on safely accessed values
        precipitation = prec_now if pd.notnull(prec_now) else prec_fore

        times.append(
            {
                "time": str(df.index[df.index.get_loc(i)]),  # Use df.index to get the actual Timestamp
                "yr_symbol": yr_symbol,
                "wl_symbol": wl_symbol,
                "prec_nowcast": prec_now,
                "prec_forecast": prec_fore,
                "precipitation": precipitation,  # Combined precipitation field
                "prob_of_prec": prob_of_prec,
                "wind_gust": wind_gust,
                "rgb": rgb_color,
                "hex": hex_color,
            }
        )
    return times


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
) -> list[list[dict[str, Any]]]:
    """
    Processes forecast data for all requested segments.

    :param segments_data: List of segment definitions from validation.
    :param colormap: Name of the colormap to use.
    :param dev: Flag to use development data.
    :param cm_preview: Flag to return colormap preview instead of weather data.
    :return: List of lists, where each inner list contains the processed time slot data for a segment.
    :raises Exception: Propagates exceptions from underlying calls like _get_segment_data.
    """
    output_segments_data = []
    # TODO: Consider running segments concurrently using asyncio.gather
    # tasks = []
    # for segment in segments_data:
    #     task = asyncio.create_task(_get_segment_data(
    #         lat=segment["lat"],
    #         lon=segment["lon"],
    #         slot_minutes=segment["slot_minutes"],
    #         slot_count=segment["led_count"],
    #         colormap_name=colormap,
    #         dev=dev,
    #     ))
    #     tasks.append(task)
    # results = await asyncio.gather(*tasks, return_exceptions=True) # Handle potential errors in tasks

    # for i, result in enumerate(results):
    #     if isinstance(result, Exception):
    #         logging.error(f"Error processing segment {segments_data[i]}: {result}")
    #         # Decide how to handle failed segments (e.g., skip, return error marker)
    #         # For now, re-raising the first encountered exception
    #         raise result # Or handle more gracefully
    #     else:
    #         segment_times = result
    #         if segments_data[i]["reversed"]:
    #             segment_times = segment_times[::-1]
    #         output_segments_data.append(segment_times)

    # Sequential processing (original logic):
    for segment in segments_data:
        logging.debug(f"Processing segment: {segment}")
        try:
            # Handle colormap preview mode
            if cm_preview:
                segment_times = _create_colormap_preview(colormap, segment["led_count"])
                # Reverse the segment if requested
                if segment["reversed"]:
                    segment_times = segment_times[::-1]
                output_segments_data.append(segment_times)
                logging.debug(f"Generated colormap preview for segment index {segment['index']}")
                continue  # Move to the next segment

            # Handle "dark" segments directly
            if segment["program"] == "dark":
                dark_segment_times = []
                for _ in range(segment["led_count"]):
                    # Create a dictionary representing a dark LED slot
                    dark_segment_times.append(
                        {
                            "time": None,  # No specific timestamp needed
                            "yr_symbol": None,
                            "wl_symbol": "dark",  # Specific symbol for WLED state if needed
                            "prec_nowcast": None,
                            "prec_forecast": None,
                            "precipitation": None,
                            "prob_of_prec": None,
                            "wind_gust": None,
                            "rgb": [0, 0, 0],
                            "hex": "000000",
                        }
                    )
                output_segments_data.append(dark_segment_times)
                logging.debug(f"Generated dark segment data for segment index {segment['index']}")
                continue  # Move to the next segment

            # Process normal segments by fetching weather data
            segment_times = await _get_segment_data(
                lat=segment["lat"],
                lon=segment["lon"],
                slot_minutes=segment["slot_minutes"],
                slot_count=segment["led_count"],
                colormap_name=colormap,
                dev=dev,
            )
            # Reverse the segment if requested (only for non-dark segments)
            if segment["reversed"]:
                segment_times = segment_times[::-1]
            output_segments_data.append(segment_times)
        except Exception as e:
            logging.exception(f"Error processing segment {segment}: {e}")
            # Re-raise the exception to be caught by the main handler
            raise Exception(f"Failed to process segment data for {segment['lat']},{segment['lon']}") from e

    return output_segments_data


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


def _format_json(processed_data: list[list[dict[str, Any]]]) -> str:
    """Formats the processed segment data into a standard JSON string, replacing NaN with null."""
    nan_found_tracker = [False]  # Use a list to make it mutable across calls
    data_without_nan = _replace_nan_with_none(processed_data, nan_found_tracker)

    if nan_found_tracker[0]:
        logging.debug("NaN values found and replaced with null in JSON output.")

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
    # _ = 1 / 0 # POISTETTU TESTIVIRHE
    # No try-except block here for _process_segments.
    # Let exceptions propagate to the generic exception handler.
    processed_segments_data = await _process_segments(segments_data, colormap, dev, cm_preview)
    logging.debug(f"Processed data for {len(processed_segments_data)} segments.")

    # Format the data based on the requested format
    if response_format == "html":
        content = _format_html(processed_segments_data)
        return HTMLResponse(content)
    elif response_format == "json":
        content = _format_json(processed_segments_data)
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

# Define the path to the HTML file relative to this script's location
# Assumes web/ is one level up from server/endpoint/
HTML_FILE_PATH = os.path.join(os.path.dirname(__file__), "web", "index.html")


async def serve_ui(request: Request) -> Response:
    """Serves the index.html file for the UI."""
    return FileResponse(HTML_FILE_PATH)


routes = [
    Route(path, endpoint=v2, methods=["GET", "POST", "HEAD"]),
    Route(path + "/ui", endpoint=serve_ui, methods=["GET"]),  # Add UI route
]

debug = True if os.getenv("DEBUG") else False

app = Starlette(debug=debug, routes=routes)


# --- Error Dumping Function ---
async def dump_error_to_file(request: Request | None, exc: Exception):
    """Dumps detailed error information to a timestamped file."""
    logging.info("Entered dump_error_to_file")
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    # Use environment variable for dump directory or default
    dump_dir = os.getenv("ERROR_DUMP_DIR", "error_dumps")
    filename = f"error_dump_{timestamp}.log"
    try:
        os.makedirs(dump_dir, exist_ok=True)
        filepath = os.path.join(dump_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(f"Timestamp: {timestamp}\n")
            f.write(f"Exception Type: {type(exc).__name__}\n")
            f.write(f"Exception Message: {exc}\n\n")

            f.write("--- Request Details ---\n")
            if request:
                f.write(f"URL: {request.url}\n")
                f.write(f"Method: {request.method}\n")
                # Avoid logging potentially sensitive headers unless explicitly needed/allowed
                # f.write(f"Headers: {dict(request.headers)}\n")
                client_host = request.client.host if request.client else "N/A"
                f.write(f"Client Host: {client_host}\n")
                # Safely attempt to read body - might fail or be large
                try:
                    # Limit body size to prevent huge logs
                    max_body_log_size = 1024 * 10  # 10 KB limit
                    body_bytes = await request.body()
                    f.write("Body:\n")  # Add newline after "Body:" label
                    if len(body_bytes) > max_body_log_size:
                        f.write(f"(Truncated, size={len(body_bytes)} > {max_body_log_size})\n")
                        f.write(body_bytes[:max_body_log_size].decode(errors="replace"))
                        f.write("...\n")  # Add newline after truncated body
                    else:
                        f.write(body_bytes.decode(errors="replace"))
                        f.write("\n")  # Add newline after full body
                except Exception as body_exc:
                    f.write(f"(Error reading body: {body_exc})\n")
            else:
                f.write("Request object not available.\n")

            f.write("\n--- Traceback ---\n")
            traceback.print_exc(file=f)

        logging.info(f"Error details dumped to {filepath}")
    except Exception as dump_exc:
        # Log error during dumping process itself
        logging.error(f"CRITICAL: Failed to dump error details to {filename}. Dump Error: {dump_exc}", exc_info=True)
        # Optionally log the original exception here as well if dumping failed
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
            sentry_sdk.flush(timeout=5.0)  # Give 5 seconds for flush
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

print("Sentry enabled:", sentry_enabled, "Sentry DSN:", sentry_dsn)
# Remove the old optional generic handler registration comment block if it exists
# # app.add_exception_handler(Exception, generic_exception_handler) # Optional: Catch all other exceptions
