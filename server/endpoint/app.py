import io
import logging
import os
from logging.config import dictConfig
from typing import Tuple

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response, StreamingResponse
from starlette.routing import Route

import yranalyzer

# TODO: check handler, perhaps not wsgi?
dictConfig(
    {
        "version": 1,
        "formatters": {
            "default": {
                "format": "[%(asctime)s] %(levelname)s in %(module)s: %(message)s"
            }
        },
        "handlers": {
            "wsgi": {"class": "logging.StreamHandler", "formatter": "default"}
        },
        "root": {"level": os.getenv("LOG_LEVEL", "INFO"), "handlers": ["wsgi"]},
    }
)


def validate_args(request: Request) -> Tuple[float, float, int, int, str, str]:
    """
    Validate query parameters.

    :param request: starlette.requests.Request
    :return: lat, lon and response format
    """
    response_format = request.query_params.get("format", "bin")
    colormap = request.query_params.get("colormap", "plywood")
    try:
        lat = float(request.query_params.get("lat"))
        lon = float(request.query_params.get("lon"))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid lat/lon values")
    try:
        slot_minutes = int(request.query_params.get("interval", 30))
        slot_count = int(request.query_params.get("slots", 16))
        if slot_minutes / 60 * slot_count > 48:
            raise HTTPException(status_code=400, detail="Interval*slots > 48 hou")
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid interval/slots values")
    return lat, lon, slot_minutes, slot_count, colormap, response_format


async def v1(request: Request) -> Response:
    """
    Get rain forecast from YR API and return html, json or binary response.

    :param request: starlette.requests.Request
    :return: Response
    """
    lat, lon, slot_minutes, slot_count, colormap, response_format = validate_args(
        request
    )
    logging.debug(f"Requested {lat} {lon} {response_format}")
    x = await yranalyzer.create_output(
        lat,
        lon,
        slot_minutes=slot_minutes,
        slot_count=slot_count,
        colormap_name=colormap,
        _format=response_format,
    )
    if response_format == "html":  # for debugging purposes
        return HTMLResponse(x)
    elif response_format == "json":  # if you want to use the data in external app
        return Response(x, media_type="application/json")
    else:  # for ESP8266 Weather lamp
        return StreamingResponse(io.BytesIO(x), media_type="application/octet-stream")


routes = [
    Route("/v1", endpoint=v1, methods=["GET", "POST", "HEAD"]),
]

debug = True if os.getenv("DEBUG") else False

app = Starlette(debug=debug, routes=routes)
