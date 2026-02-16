import argparse
import dataclasses
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

# Cache TTL in seconds
CACHE_TTL_SECONDS = 2 * 60

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


@dataclasses.dataclass
class CacheResult:
    """Result of a cache-aware data fetch from YR API.

    Args:
        data: The YR API response data, or None if no data is available.
        cache_age_seconds: Age of the cache file in seconds, or None if no cache existed.
        source: Origin of the data - one of "api", "cache_fresh", "cache_stale", "none".
    """

    data: dict | None
    cache_age_seconds: float | None
    source: str  # "api", "cache_fresh", "cache_stale", "none"


def _is_valid_yr_response(data: dict) -> bool:
    """Check that a YR API response has the expected structure.

    Args:
        data: Parsed JSON response from YR API.

    Returns:
        True if the response contains properties.timeseries, False otherwise.
    """
    try:
        timeseries = data["properties"]["timeseries"]
        return isinstance(timeseries, list) and len(timeseries) > 0
    except (KeyError, TypeError):
        return False


def _get_cache_path(lat: float, lon: float, cast_type: str) -> pathlib.Path:
    """Build the cache file path for given coordinates and cast type.

    Args:
        lat: Latitude.
        lon: Longitude.
        cast_type: One of "nowcast" or "locationforecast".

    Returns:
        Path to the cache file.
    """
    cachedir = pathlib.Path("cache")
    cachedir.mkdir(parents=True, exist_ok=True)
    return cachedir / f"yr-cache-{cast_type}.{lat}_{lon}.json"


async def check_cache(
    lat: float, lon: float, cast_type: str = "locationforecast", dev: bool = False
) -> tuple[pathlib.Path, dict | None, float | None]:
    """Read YR data from cache file. Never deletes stale files.

    Returns the cache path, loaded data (if fresh), and age in seconds.
    Stale files are kept on disk for fallback use.

    Args:
        lat: Latitude.
        lon: Longitude.
        cast_type: One of "nowcast" or "locationforecast".
        dev: If True, use local sample response data with fresh timestamps.

    Returns:
        Tuple of (cache_path, data_if_fresh_or_None, age_seconds_or_None).
    """
    if dev:
        now = datetime.datetime.now(datetime.UTC)
        if cast_type == "locationforecast":
            delta = 60
            ts = now.replace(minute=0, second=0, microsecond=0)
        else:
            delta = 5
            ts = now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)
        cachefile = pathlib.Path(f"yr-cache-{cast_type}.dev.json")
        with open(cachefile) as f:
            yrdata = json.loads(f.read())
            for t in yrdata["properties"]["timeseries"]:
                newtime = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
                ts = ts + datetime.timedelta(minutes=delta)
                t["time"] = newtime
        return cachefile, yrdata, 0.0

    cachefile = _get_cache_path(lat, lon, cast_type)

    if not cachefile.exists():
        return cachefile, None, None

    mtime = datetime.datetime.fromtimestamp(cachefile.stat().st_mtime)
    now = datetime.datetime.now()
    age = (now - mtime).total_seconds()

    if age <= CACHE_TTL_SECONDS:
        logging.info(f"Using fresh cached data from {cachefile} (age: {age:.0f}s).")
        with open(cachefile) as f:
            yrdata = json.loads(f.read())
        return cachefile, yrdata, age

    # Stale cache — do NOT delete, return None for data so caller attempts API
    logging.info(f"Cache file {cachefile} is stale (age: {age:.0f}s). Will attempt API refresh.")
    return cachefile, None, age


def _load_stale_cache(cachefile: pathlib.Path) -> dict | None:
    """Attempt to load a stale cache file as fallback.

    Args:
        cachefile: Path to the cache file.

    Returns:
        Parsed JSON data if the file exists and is valid, None otherwise.
    """
    if not cachefile.exists():
        return None
    try:
        with open(cachefile) as f:
            data = json.loads(f.read())
        if _is_valid_yr_response(data):
            return data
        logging.warning(f"Stale cache file {cachefile} has invalid structure.")
    except (json.JSONDecodeError, OSError) as e:
        logging.warning(f"Failed to load stale cache {cachefile}: {e}")
    return None


async def get_yrdata(lat: float, lon: float, cast_type: str = "locationforecast", dev: bool = False) -> CacheResult:
    """Fetch YR data with cache-first strategy and stale fallback.

    1. Check cache — if fresh, return immediately.
    2. Attempt API call.
    3. On API success: validate, write cache, return.
    4. On API error: fall back to stale cache if available.
    5. Total failure: return CacheResult with source="none".

    Args:
        lat: Latitude.
        lon: Longitude.
        cast_type: One of "nowcast" or "locationforecast".
        dev: If True, use local sample response data.

    Returns:
        CacheResult with data, cache age, and source indicator.
    """
    cachefile, yrdata, age = await check_cache(lat, lon, cast_type, dev)

    # Fresh cache hit
    if yrdata is not None:
        return CacheResult(data=yrdata, cache_age_seconds=age, source="cache_fresh")

    # Attempt API call
    parameters = f"lat={lat:.3f}&lon={lon:.3f}"
    headers = {"User-Agent": USER_AGENT}
    url = API_URL.format(cast_type)
    full_url = f"{url}?{parameters}"
    logging.info(f"Requesting data from {full_url}")

    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(full_url, headers=headers)
    except RequestError as err:
        logging.critical(f"Network error requesting {full_url}: {err}")
        # Fall through to stale fallback
        return _stale_or_none(cachefile, age)

    if res.status_code == HTTP_OK:
        logging.info("Got 200 OK")
    elif res.status_code == HTTP_NON_AUTHORITATIVE_INFORMATION:
        logging.warning("Got 203, read the docs")
    elif res.status_code == HTTP_UNPROCESSABLE_ENTITY:
        logging.warning("Got 422, data is not available!")
        return _stale_or_none(cachefile, age)
    else:
        logging.warning(f"Got {res.status_code}!")
        return _stale_or_none(cachefile, age)

    # Parse response
    try:
        yrdata = json.loads(res.text)
    except json.JSONDecodeError:
        logging.error(f"Invalid JSON from {full_url}")
        return _stale_or_none(cachefile, age)

    # Validate structure
    if not _is_valid_yr_response(yrdata):
        logging.error(f"Invalid YR response structure from {full_url}")
        return _stale_or_none(cachefile, age)

    # Write to cache
    logging.info(f"Caching data to {cachefile}")
    with open(cachefile, "w") as f:
        f.write(res.text)

    # Write to history
    now = datetime.datetime.now(datetime.UTC)
    historydir = pathlib.Path("history") / now.strftime("%Y-%m-%d")
    historydir.mkdir(parents=True, exist_ok=True)
    ts = now.strftime("%Y%m%dT%H%M%SZ")
    historyfile = historydir / f"yr-{cast_type}-{lat}_{lon}-{ts}.json"
    if not historyfile.exists():
        with open(historyfile, "w") as f:
            f.write(res.text)

    logging.debug(res.headers)
    return CacheResult(data=yrdata, cache_age_seconds=0, source="api")


def _stale_or_none(cachefile: pathlib.Path, age: float | None) -> CacheResult:
    """Try stale cache fallback, or return empty result.

    Args:
        cachefile: Path to the cache file.
        age: Age of the cache file in seconds, or None if no file.

    Returns:
        CacheResult with stale data or source="none".
    """
    stale_data = _load_stale_cache(cachefile)
    if stale_data is not None:
        logging.warning(f"Serving stale cache from {cachefile} (age: {age:.0f}s).")
        return CacheResult(data=stale_data, cache_age_seconds=age, source="cache_stale")
    logging.error(f"No data available for {cachefile} — API failed and no stale cache.")
    return CacheResult(data=None, cache_age_seconds=None, source="none")


async def get_locationforecast(lat: float, lon: float, dev: bool) -> CacheResult:
    """Fetch location forecast from YR API with cache fallback.

    Args:
        lat: Latitude.
        lon: Longitude.
        dev: If True, use local sample response data.

    Returns:
        CacheResult with forecast data.

    Raises:
        ValueError: If coordinates are out of valid range.
    """
    if MIN_LAT < lat < MAX_LAT and MIN_LON < lon < MAX_LON:
        return await get_yrdata(lat, lon, "locationforecast", dev)
    raise ValueError(f"Values must be '{MIN_LAT} < lat < {MAX_LAT} and {MIN_LON} < lon < {MAX_LON}'")


async def get_nowcast(lat: float, lon: float, dev: bool) -> CacheResult:
    """Request nowcast from YR API if coordinates are within nowcast coverage.

    Args:
        lat: Latitude.
        lon: Longitude.
        dev: If True, use local sample response data.

    Returns:
        CacheResult with nowcast data, or CacheResult(None, None, "none") if outside coverage.
    """
    nowcast_coverage = wkt.loads(NOWCAST_COVERAGE_WKT)
    if nowcast_coverage.contains(Point(lon, lat)):
        return await get_yrdata(lat, lon, "nowcast", dev)
    return CacheResult(data=None, cache_age_seconds=None, source="none")


async def main(lat: float = 60.17, lon: float = 24.95) -> None:
    """Run a test fetch and print results.

    Args:
        lat: Latitude.
        lon: Longitude.
    """
    result = await get_nowcast(lat, lon, False)
    print(json.dumps(result.data, indent=2))


if __name__ == "__main__":
    import asyncio

    logging.basicConfig(level=logging.DEBUG)

    parser = argparse.ArgumentParser(description="Fetch weather data from YR API.")
    parser.add_argument("--lat", type=float, default=60.17, help="Latitude")
    parser.add_argument("--lon", type=float, default=24.95, help="Longitude")
    args = parser.parse_args()

    asyncio.run(main(args.lat, args.lon))
