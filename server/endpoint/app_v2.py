import io
import json
import logging
import os
import re
from collections import OrderedDict
from logging.config import dictConfig
from typing import Any

import pandas as pd
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
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

# TODO: these should be in some configuration file
MAX_FORECAST_DURATION_HOURS = 48
SEGMENT_PARTS = 6
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


def validate_args_v2(request: Request) -> tuple[str, str, bool, list[dict]]:
    """
    Validate query parameters for V2 endpoint.

    Parses common parameters (format, colormap, dev) and segment data from the 's' query parameter.
    The 's' parameter contains one or more segments separated by '+' or ' '.
    Each segment format: index,program,led_count,reversed,lat,lon
    Example: s=1,r5min,12,0,60.167,24.951+2,r15min,8,1,60.167,24.951

    :param request: starlette.requests.Request
    :return: tuple containing response_format, colormap, dev flag, and a list of segment dictionaries.
             Each segment dictionary contains: index, program, led_count, reversed, lat, lon, slot_minutes.
    :raises HTTPException: if parameters are invalid or missing.
    """
    response_format = request.query_params.get("format", "json_wled")
    colormap = request.query_params.get("colormap", "plain")
    dev = request.query_params.get("dev") is not None

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

            # Derive slot_minutes from program string (e.g., "r5min", "program15min")
            match = re.search(r"(\d+)min$", program)  # Use search and match digits followed by 'min' at the end
            if not match:
                # Handle other potential program types or raise error
                raise ValueError(
                    f"Invalid program format: '{program}'. Expected format ending like '5min', '15min' etc."
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

    return response_format, colormap, dev, segments_data


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
        color_data = df.loc[i, "color"]
        if isinstance(color_data, list) and len(color_data) == 3:
            hex_color = f"{color_data[0]:02x}{color_data[1]:02x}{color_data[2]:02x}"
            rgb_color = color_data
        else:
            logging.warning(
                f"Invalid or missing color data for index {i} at {lat},{lon}. Using default [0,0,0]. Data: {color_data}"
            )
            hex_color = "000000"
            rgb_color = [0, 0, 0]  # Default to black

        times.append(
            {
                "time": str(df.index[df.index.get_loc(i)]),  # Use df.index to get the actual Timestamp
                "yr_symbol": df.loc[i, "symbol"],  # Added yr_symbol for completeness if needed later
                "wl_symbol": df.loc[i, "wl_symbol"],
                "prec_nowcast": df.loc[i, "prec_now"],
                "prec_forecast": df.loc[i, "prec_fore"],
                "precipitation": precipitation,  # Combined precipitation field
                "prob_of_prec": df.loc[i, "prob_of_prec"],
                "wind_gust": df.loc[i, "wind_gust"],
                "rgb": rgb_color,
                "hex": hex_color,
            }
        )
    return times


async def _process_segments(
    segments_data: list[dict[str, Any]], colormap: str, dev: bool
) -> list[list[dict[str, Any]]]:
    """
    Processes forecast data for all requested segments.

    :param segments_data: List of segment definitions from validation.
    :param colormap: Name of the colormap to use.
    :param dev: Flag to use development data.
    :return: List of lists, where each inner list contains the processed time slot data for a segment.
    """
    output_segments_data = []
    for segment in segments_data:
        segment_times = await _get_segment_data(
            lat=segment["lat"],
            lon=segment["lon"],
            slot_minutes=segment["slot_minutes"],
            slot_count=segment["led_count"],
            colormap_name=colormap,
            dev=dev,
        )
        if segment["reversed"]:
            segment_times = segment_times[::-1]
        output_segments_data.append(segment_times)
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

    for i, segment_data in enumerate(processed_data):
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
            headers = [
                "time",
                "yr_symbol",
                "wl_symbol",
                "prec_nowcast",
                "prec_forecast",
                "precipitation",
                "prob_of_prec",
                "wind_gust",
                "hex",
            ]
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


def _format_json(processed_data: list[list[dict[str, Any]]]) -> str:
    """Formats the processed segment data into a standard JSON string."""
    # The data is already in the desired list-of-lists-of-dicts structure
    return json.dumps(processed_data)


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

    :param request: starlette.requests.Request
    :return: Response
    """
    response_format, colormap, dev, segments_data = validate_args_v2(request)
    logging.debug(
        f"Request validated: format={response_format}, colormap={colormap}, dev={dev}, segments={len(segments_data)}"
    )

    try:
        # Process data for all segments
        processed_segments_data = await _process_segments(segments_data, colormap, dev)
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
            # Should not happen if validation is correct, but handle defensively
            logging.error(f"Unsupported response format requested: {response_format}")
            raise HTTPException(
                status_code=400,
                detail={"error_code": "INVALID_FORMAT", "message": f"Unsupported format: {response_format}"},
            )

    except Exception as e:
        logging.exception(f"Error during request processing in v2 endpoint: {e}")
        # Re-raise as HTTPException or return a generic error response
        # Using the custom handler, raising HTTPException is cleaner
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "PROCESSING_ERROR",
                "message": "An internal error occurred while processing the request.",
            },
        ) from e


path = os.getenv("ENDPOINT_PATH", "/v2")

routes = [
    Route(path, endpoint=v2, methods=["GET", "POST", "HEAD"]),
]

debug = True if os.getenv("DEBUG") else False

app = Starlette(debug=debug, routes=routes)


# Custom exception handler for HTTPException
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """
    Custom exception handler to return JSON errors for HTTPException.
    """
    if isinstance(exc.detail, dict):
        # If detail is a dictionary, use its structure
        error_content = {
            "error_code": exc.detail.get("error_code", f"HTTP_{exc.status_code}"),
            "message": exc.detail.get("message", "An error occurred."),
        }
        if "details" in exc.detail:
            error_content["details"] = exc.detail["details"]
    else:
        # Fallback for string details
        error_content = {"error_code": f"HTTP_{exc.status_code}", "message": exc.detail}

    # Log the error that triggered the handler
    logging.error(f"HTTP Exception: Status={exc.status_code}, Detail={exc.detail}", exc_info=exc if debug else False)

    return JSONResponse(status_code=exc.status_code, content=error_content)


# Register the custom exception handler
app.add_exception_handler(HTTPException, http_exception_handler)
# Add handler for generic exceptions as well during debug?
# async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
#     logging.exception(f"Unhandled exception: {exc}", exc_info=exc) # Log with stack trace
#     return JSONResponse(
#         status_code=500,
#         content={
#             "error_code": "INTERNAL_SERVER_ERROR",
#             "message": "An unexpected error occurred.",
#             "details": str(exc) if debug else None # Show details only in debug mode
#         }
#     )
# app.add_exception_handler(Exception, generic_exception_handler) # Optional: Catch all other exceptions
