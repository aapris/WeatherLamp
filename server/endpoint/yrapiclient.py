import datetime
import json
import logging
import pathlib
from typing import Optional, Tuple

import httpx
from shapely import wkt
from shapely.geometry import Point

API_URL: str = "https://api.met.no/weatherapi/{}/2.0/complete"
USER_AGENT: str = "WeatherLamp/0.3 github.com/aapris/WeatherLamp"

# Coverage is taken from file
# https://api.met.no/weatherapi/nowcast/2.0/coverage.zip
# then simplified and shrunk using negative buffer:
# obj.simplify(1).buffer(-1).simplify(1)
nowcast_coverage_wkt = """POLYGON ((
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
        lat: float, lon: float, cast_type: str = "locationforecast"
) -> Tuple[pathlib.Path, Optional[dict]]:
    """
    Read YR data from a file if it exists for requested lat and lon and it is not more than 5 minutes old.

    :param lat: float latitude
    :param lon: float longitude
    :param cast_type: one of "nowcast" or "locationforecast"
    :return: path to cache file and the data if there was cache hit
    """
    yrdata = None
    cachedir = "cache"
    pathlib.Path(cachedir).mkdir(parents=True, exist_ok=True)
    cachefile = pathlib.Path(cachedir).joinpath(pathlib.Path(f"yr-cache-{cast_type}.{lat}_{lon}.json"))
    if cachefile.exists():
        mtime = datetime.datetime.fromtimestamp(cachefile.stat().st_mtime)
        now = datetime.datetime.now()
        age = (now - mtime).total_seconds()
        if age > 5 * 60:
            logging.info(f"Removing {cachefile} which is {age} seconds old.")
            cachefile.unlink(missing_ok=True)
    if cachefile.exists():
        logging.info(f"Using cached data from {cachefile}.")
        with open(cachefile, "rt") as f:
            yrdata = json.loads(f.read())
    return cachefile, yrdata


async def get_yrdata(lat: float, lon: float, cast_type: str = "locationforecast"):
    cachefile, yrdata = await check_cache(lat, lon, cast_type)
    if yrdata is None:
        parameters = f"lat={lat}&lon={lon}"
        headers = {"User-Agent": USER_AGENT}
        url = API_URL.format(cast_type)
        full_url = f"{url}?{parameters}"
        logging.info(f"Requesting data from {full_url}")
        async with httpx.AsyncClient() as client:
            res = await client.get(full_url, headers=headers)
        if res.status_code == 200:
            logging.info(f"Got 200 OK")
        elif res.status_code == 203:
            logging.warning(f"Got 203, read the docs")
        elif res.status_code == 422:
            logging.warning(f"Got 422, data is not available!")
        else:
            logging.warning(f"Got {res.status_code}!")
        logging.info(f"Caching data to {cachefile}")
        try:
            yrdata = json.loads(res.text)
        except json.decoder.JSONDecodeError:
            cachefile += ".error"
        with open(cachefile, "wt") as f:
            f.write(res.text)
        logging.debug(res.headers)
    return yrdata


async def get_locationforecast(lat: float, lon: float) -> Optional[dict]:
    yrdata = None
    if -90 < lat < 90 and -180 < lon < 180:
        yrdata = await get_yrdata(lat, lon, "locationforecast")
    else:
        raise ValueError("Values must be '-90 < lat < 90 and -180 < lon < 180'")
    return yrdata


async def get_nowcast(lat: float, lon: float) -> Optional[dict]:
    nowcast_coverage = wkt.loads(nowcast_coverage_wkt)
    yrdata = None
    if nowcast_coverage.contains(Point(lon, lat)):
        yrdata = await get_yrdata(lat, lon, "nowcast")
    return yrdata


async def main(lat: float = 60.17, lon: float = 24.95):
    data = await get_nowcast(lat, lon)
    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    import sys
    import asyncio

    logging.basicConfig(level=logging.DEBUG)

    if len(sys.argv) == 3:
        asyncio.run(main(float(sys.argv[1]), float(sys.argv[2])))
    else:
        asyncio.run(main())
