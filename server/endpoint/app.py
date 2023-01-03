import io
import itertools
import json
import logging
import os
from collections import OrderedDict
from logging.config import dictConfig
from typing import Tuple, Union

import pandas as pd
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response, StreamingResponse
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

COLORMAPS["plywood"] = {
    "CLEARSKY": [20, 108, 214],
    "PARTLYCLOUDY": [40, 158, 154],
    "CLOUDY": [70, 200, 140],
    "LIGHTRAIN_LT50": [110, 180, 1],
    "LIGHTRAIN": [90, 200, 1],
    "RAIN": [202, 252, 1],
    "HEAVYRAIN": [173, 133, 2],
    "VERYHEAVYRAIN": [143, 93, 2],
}


def validate_args(request: Request) -> Tuple[float, float, int, int, str, str, bool]:
    """
    Validate query parameters.

    :param request: starlette.requests.Request
    :return: lat, lon and response format
    """
    response_format = request.query_params.get("format", "bin")
    colormap = request.query_params.get("colormap", "plain")
    dev = True if request.query_params.get("dev") is not None else False
    try:
        lat = float(request.query_params.get("lat"))
        lon = float(request.query_params.get("lon"))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid lat/lon values")
    try:
        buttoncount = int(request.query_params.get("buttoncount", 0)) % 4
        if buttoncount == 0:
            slot_minutes = int(request.query_params.get("interval", 30))
        elif buttoncount == 1:
            slot_minutes = 15
        elif buttoncount == 2:
            slot_minutes = 5
        else:
            slot_minutes = 60  # spot price
        slot_count = int(request.query_params.get("slots", 16))
        if slot_minutes / 60 * slot_count > 48:
            raise HTTPException(status_code=400, detail="Interval*slots > 48 hours")
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid interval/slots values")
    return lat, lon, slot_minutes, slot_count, colormap, response_format, dev


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
    assert len(df.index) == slot_count
    return df


async def create_output(
        lat: float,
        lon: float,
        _format: str = "bin",
        slot_minutes: int = 30,
        slot_count: int = 16,
        colormap_name: str = "plain",
        output: str = None,
        dev: bool = False,
) -> Union[str, bytearray]:
    """
    Create output in requested format.

    :param lat: latitude
    :param lon: longitude
    :param slot_minutes: minutes for pandas resample function
    :param slot_count: how many slot_count will be returned
    :param colormap_name: pre-defined color map [plain or plywood]
    :param _format: output format [html, json or bin]
    :param output: optional output file
    :param dev: use local sample response data instead of remote API
    :return: precipitation data in requested format
    """
    df = await create_forecast(lat, lon, slot_minutes, slot_count, dev)
    colors = []
    times = []
    if colormap_name in COLORMAPS:
        colormap = COLORMAPS[colormap_name]
    else:
        colormap = COLORMAPS[list(COLORMAPS.keys())[0]]
    cnt = 0
    df = yranalyzer.add_symbol_and_color(df, colormap)
    df = yranalyzer.add_day_night(df, lat, lon)
    pd.set_option("display.max_rows", None, "display.max_columns", None, "display.width", 1000)
    logging.info("\n" + str(df))

    for i in df.index:
        # Take always nowcast's precipitation, it should be the most accurate
        if pd.notnull(df["prec_now"][i]):
            precipitation = df["prec_now"][i]
        else:
            precipitation = df["prec_fore"][i]
            logging.debug(
                "{} {} {} {} {} {} {}".format(
                    precipitation,
                    df["prec_now"][i],
                    df["prec_fore"][i],
                    df["prob_of_prec"][i],
                    df["symbol"][i],
                    df["wl_symbol"][i],
                    df["color"][i],
                )
            )
        colors += df["color"][i] + [int(df["wind_gust"][i])]  # R, G, B, wind gust speed
        times.append(
            {
                "time": str(i),
                "wl_symbol": df["wl_symbol"][i],
                "yr_symbol": df["symbol"][i],
                "prec_nowcast": df["prec_now"][i],
                "prec_forecast": df["prec_fore"][i],
                "prob_of_prec": df["prob_of_prec"][i],
                "wind_gust": df["wind_gust"][i],
                "rgb": df["color"][i],
            }
        )
        cnt += 1
    assert len(colors) == slot_count * 4
    arr = bytearray(colors)
    reverse = True  # TODO: add option to use reversed_arr
    if reverse:
        # Split list to a chunks of 4
        split_arr = list([arr[i: i + 4] for i in range(0, len(arr), 4)])
        # Reverse chunks
        reversed_split_arr = list(reversed(split_arr))
        # Join chunks back to 1-dim array
        arr = bytearray((itertools.chain.from_iterable(reversed_split_arr)))
    if output is not None:
        with open(output, "wb") as f:
            f.write(arr)
    if _format == "json":
        return json.dumps(times, indent=2)
    elif _format == "html":
        html = [
            """<html><head>
        <style>
          body {
            margin: 0px;
            padding: 0px;
          }
          .container {
            width: 100%;
            min-height: 100%;
            padding: 0px;
          }
        </style>
        </head><body><table class="container">\n""",
            """<tr>
        <td>time</td>
        <td>yr_symbol</td>
        <td>wl_symbol</td>
        <td>prec</td>
        <td>gust</td>
        </tr>""",
        ]
        for t in times:
            t["formatted_color"] = "rgb({})".format(",".join([str(x) for x in t["rgb"]]))
            html.append(
                """<tr style='background-color: {formatted_color}'>
            <td>{time}</td>
            <td>{yr_symbol}</td>
            <td>{wl_symbol}</td>
            <td>{prec_nowcast}/{prec_forecast}</td>
            <td>{wind_gust}</td>
            </tr>""".format(
                    **t
                )
            )
        html.append("</table></html>")
        return "\n".join(html)
    else:  # format == "bin":
        return arr


async def v1(request: Request) -> Response:
    """
    Get rain forecast from YR API and return html, json or binary response.

    :param request: starlette.requests.Request
    :return: Response
    """
    lat, lon, slot_minutes, slot_count, colormap, response_format, dev = validate_args(request)
    logging.debug(f"Requested {lat} {lon} {response_format}")
    x = await create_output(
        lat,
        lon,
        slot_minutes=slot_minutes,
        slot_count=slot_count,
        colormap_name=colormap,
        _format=response_format,
        dev=dev,
    )
    if response_format == "html":  # for debugging purposes
        return HTMLResponse(x)
    elif response_format == "json":  # if you want to use the data in external app
        return Response(x, media_type="application/json")
    else:  # for ESP8266 Weather lamp
        return StreamingResponse(io.BytesIO(x), media_type="application/octet-stream")


path = os.getenv("ENDPOINT_PATH", "/v1")

routes = [
    Route(path, endpoint=v1, methods=["GET", "POST", "HEAD"]),
]

debug = True if os.getenv("DEBUG") else False

app = Starlette(debug=debug, routes=routes)
