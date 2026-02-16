import argparse
import datetime
import json
import logging
import pathlib

import httpx
from httpx import RequestError
from shapely import wkt
from shapely.geometry import Point

API_URL: str = "https://api.met.no/weatherapi/{}/2.0/complete"
USER_AGENT: str = "WeatherLamp/0.4 github.com/aapris/WeatherLamp"

# HTTP Status Codes
HTTP_OK = 200
HTTP_NON_AUTHORITATIVE_INFORMATION = 203
HTTP_UNPROCESSABLE_ENTITY = 422

# Define coordinate boundaries
MIN_LAT = -90.0
MAX_LAT = 90.0
MIN_LON = -180.0
MAX_LON = 180.0

# Coverage is taken from file
# https://api.met.no/weatherapi/nowcast/2.0/coverage.zip
# then simplified and shrunk using negative buffer:
# obj.simplify(1).buffer(-1).simplify(1)
NOWCAST_COVERAGE_WKT = """POLYGON ((
    2.547779705832076 53.30271492607023,
    -2.905815348621908 64.65327205671177,
    -9.497201603182553 71.32483641294951,
    15.01761974015538 72.85721223563839,
    39.50028754686385 71.32462086941165,
    32.90812282213389 64.65301564004723,
    27.45389690417179 53.30251807369419,
    2.547779705832076 53.30271492607023
))"""


async def check_cache(
    lat: float, lon: float, cast_type: str = "locationforecast", dev: bool = False
) -> tuple[pathlib.Path, dict | None]:
    """
    Read YR data from a file if it exists for requested lat and lon and
    it is not more than 5 minutes old.

    :param lat: float latitude
    :param lon: float longitude
    :param cast_type: one of "nowcast" or "locationforecast"
    :param dev:
    :return: path to cache file and the data if there was cache hit
    """
    yrdata = None

    if dev:  # In dev mode generate fresh timestamps for sample data
        now = datetime.datetime.now(datetime.UTC)
        if cast_type == "locationforecast":  # previous full hour (18:47 -> 18:00)
            delta = 60  # minutes
            ts = now.replace(minute=0, second=0, microsecond=0)
        else:  # previous full 5 minutes (18:47 -> 18:45)
            delta = 5
            ts = now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)
        cachefile = pathlib.Path(".").joinpath(pathlib.Path(f"yr-cache-{cast_type}.dev.json"))
        with open(cachefile) as f:
            yrdata = json.loads(f.read())
            for t in yrdata["properties"]["timeseries"]:
                newtime = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
                ts = ts + datetime.timedelta(minutes=delta)
                t["time"] = newtime
    else:
        cachedir = "cache"
        pathlib.Path(cachedir).mkdir(parents=True, exist_ok=True)
        cachefile = pathlib.Path(cachedir).joinpath(pathlib.Path(f"yr-cache-{cast_type}.{lat}_{lon}.json"))
        if cachefile.exists():
            mtime = datetime.datetime.fromtimestamp(cachefile.stat().st_mtime)
            now = datetime.datetime.now()
            age = (now - mtime).total_seconds()
            if age > 2 * 60:
                logging.info(f"Removing {cachefile} which is {age} seconds old.")
                cachefile.unlink(missing_ok=True)
        if cachefile.exists():
            logging.info(f"Using cached data from {cachefile}.")
            with open(cachefile) as f:
                yrdata = json.loads(f.read())
    return cachefile, yrdata


async def get_yrdata(lat: float, lon: float, cast_type: str = "locationforecast", dev: bool = False):
    cachefile, yrdata = await check_cache(lat, lon, cast_type, dev)
    if yrdata is None:
        parameters = f"lat={lat:.3f}&lon={lon:.3f}"
        headers = {"User-Agent": USER_AGENT}
        url = API_URL.format(cast_type)
        full_url = f"{url}?{parameters}"
        logging.info(f"Requesting data from {full_url}")
        async with httpx.AsyncClient() as client:
            try:
                res = await client.get(full_url, headers=headers)
            except RequestError as err:
                logging.critical(str(err))
                # Handle the error appropriately, maybe raise it or return None/empty dict
                return None
        if res.status_code == HTTP_OK:
            logging.info("Got 200 OK")
        elif res.status_code == HTTP_NON_AUTHORITATIVE_INFORMATION:
            logging.warning("Got 203, read the docs")
        elif res.status_code == HTTP_UNPROCESSABLE_ENTITY:
            logging.warning("Got 422, data is not available!")
        else:
            logging.warning(f"Got {res.status_code}!")
        logging.info(f"Caching data to {cachefile}")
        try:
            yrdata = json.loads(res.text)
        except json.decoder.JSONDecodeError:
            cachefile += ".error"
        with open(cachefile, "w") as f:
            f.write(res.text)
        # Temporarily write all files to history directory too
        now = datetime.datetime.now(datetime.UTC)
        historydir = pathlib.Path("history") / pathlib.Path(now.strftime("%Y-%m-%d"))
        pathlib.Path(historydir).mkdir(parents=True, exist_ok=True)
        ts = now.strftime("%Y%m%dT%H%M%SZ")
        historyfile = historydir / pathlib.Path(f"yr-{cast_type}-{lat}_{lon}-{ts}.json")
        if historyfile.exists() is False:
            with open(historyfile, "w") as f:
                f.write(res.text)
        logging.debug(res.headers)
    return yrdata


async def get_locationforecast(lat: float, lon: float, dev: bool) -> dict | None:
    if MIN_LAT < lat < MAX_LAT and MIN_LON < lon < MAX_LON:
        yrdata = await get_yrdata(lat, lon, "locationforecast", dev)
    else:
        raise ValueError(f"Values must be '{MIN_LAT} < lat < {MAX_LAT} and {MIN_LON} < lon < {MAX_LON}'")
    return yrdata


async def get_nowcast(lat: float, lon: float, dev: bool) -> dict | None:
    """
    Request nowcast from YR API, if lat & lon are within nowcast's coverage.

    :param lat: latitude
    :param lon: longitude
    :param dev: if True use local sample response data instead of remote API
    :return: response data
    """
    nowcast_coverage = wkt.loads(NOWCAST_COVERAGE_WKT)
    yrdata = None
    if nowcast_coverage.contains(Point(lon, lat)):
        yrdata = await get_yrdata(lat, lon, "nowcast", dev)
    return yrdata


async def main(lat: float = 60.17, lon: float = 24.95):
    data = await get_nowcast(lat, lon, False)
    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    import asyncio

    logging.basicConfig(level=logging.DEBUG)

    # Add argparse logic
    parser = argparse.ArgumentParser(description="Fetch weather data from YR API.")
    parser.add_argument("--lat", type=float, default=60.17, help="Latitude")
    parser.add_argument("--lon", type=float, default=24.95, help="Longitude")
    args = parser.parse_args()

    # Use parsed arguments
    asyncio.run(main(args.lat, args.lon))
