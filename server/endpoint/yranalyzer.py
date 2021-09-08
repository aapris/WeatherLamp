import argparse
import datetime
import json
import logging
import time

import pandas as pd
import pytz
from dateutil.parser import parse

import sys
import os

# PACKAGE_PARENT = '..'
# SCRIPT_DIR = os.path.dirname(os.path.realpath(os.path.join(os.getcwd(), os.path.expanduser(__file__))))
# sys.path.append(os.path.normpath(os.path.join(SCRIPT_DIR, PACKAGE_PARENT)))
# print(sys.path)
import yrapiclient

# TODO: these should be in some configuration file
# COLOR_CLEARSKY_NIGHT = [5, 18, 151]
COLORS = {
    "CLEARSKY_DAY": [20, 108, 214],
    "PARTLYCLOUDY": [40, 158, 154],
    "CLOUDY": [70, 200, 140],
    "LIGHTRAIN": [90, 200, 1],
    "LIGHTRAIN_GT50": [110, 180, 1],
    "RAIN": [202, 252, 1],
    "HEAVYRAIN": [173, 133, 2],
    "VERYHEAVYRAIN": [143, 93, 2],
}

# A dict to map weather symbol to particular RGB color
symbolmap = {
    **dict.fromkeys(
        [
            "clearsky",
            "fair",
        ],
        COLORS["CLEARSKY_DAY"],
    ),
    **dict.fromkeys(
        [
            "partlycloudy",
        ],
        COLORS["PARTLYCLOUDY"],
    ),
    **dict.fromkeys(
        [
            "cloudy",
            "fog",
        ],
        COLORS["CLOUDY"],
    ),
    **dict.fromkeys(
        [
            "heavyrain",
            "heavyrainandthunder",
            "heavyrainshowers",
            "heavyrainshowersandthunder",
            "heavysleet",
            "heavysleetandthunder",
            "heavysleetshowers",
            "heavysleetshowersandthunder",
            "heavysnow",
            "heavysnowandthunder",
            "heavysnowshowers",
            "heavysnowshowersandthunder",
        ],
        COLORS["HEAVYRAIN"],
    ),
    **dict.fromkeys(
        [
            "lightrain",
            "lightrainandthunder",
            "lightrainshowers",
            "lightrainshowersandthunder",
            "lightsleet",
            "lightsleetandthunder",
            "lightsleetshowers",
            "lightsnow",
            "lightsnowandthunder",
            "lightsnowshowers",
            "lightssleetshowersandthunder",
            "lightssnowshowersandthunder",
        ],
        COLORS["LIGHTRAIN"],
    ),
    **dict.fromkeys(
        [
            "rain",
            "rainandthunder",
            "rainshowers",
            "rainshowersandthunder",
            "sleet",
            "sleetandthunder",
            "sleetshowers",
            "sleetshowersandthunder",
            "snow",
            "snowandthunder",
            "snowshowers",
            "snowshowersandthunder",
        ],
        COLORS["RAIN"],
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument(
        "--log",
        dest="log",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="ERROR",
        help="Set the logging level",
    )
    parser.add_argument("--lat", required=True, help="Latitude in decimal format (dd.ddd)")
    parser.add_argument("--lon", required=True, help="Longitude in decimal format (dd.ddd)")
    parser.add_argument("--output", help="Output file name")
    parser.add_argument("--historypath", help="Where responses are stored")
    args = parser.parse_args()
    if args.log:
        logging.basicConfig(
            level=getattr(logging, args.log),
            datefmt="%Y-%m-%dT%H:%M:%S",
            format="%(asctime)s.%(msecs)03dZ %(levelname)s %(message)s",
        )
        logging.Formatter.converter = time.gmtime  # Timestamps in UTC time
    return args


def add_to_dict(dict_: dict, key: str, val: float):
    if key not in dict_:
        dict_[key] = []
    dict_[key].append(val)


def yr_precipitation_to_df(yrdata, cast):
    timeseries = yrdata["properties"]["timeseries"]
    timestamps = []
    pers = {}
    for t in timeseries:
        if cast == "now":  # nowcast has only precipitation rate
            did = t["data"]["instant"]["details"]
            add_to_dict(pers, f"prec_{cast}", did["precipitation_rate"])
        elif cast == "fore":  # forecast has more data available
            if "next_1_hours" not in t["data"]:
                break
            d1h = t["data"]["next_1_hours"]
            # Precipitation
            add_to_dict(pers, f"prec_{cast}", d1h["details"]["precipitation_amount"])
            add_to_dict(pers, "prob_of_prec", d1h["details"]["probability_of_precipitation"])
            # Weather symbol
            symbol_code = d1h["summary"]["symbol_code"]
            if symbol_code.find("_") >= 0:  # Check for _day, _night postfix
                symbol, variant = symbol_code.split("_")
            else:
                symbol, variant = symbol_code, None
            add_to_dict(pers, "symbol_code", symbol_code)
            add_to_dict(pers, "symbol", symbol)
            add_to_dict(pers, "variant", variant)
            # Wind and other forecasts
            did = t["data"]["instant"]["details"]
            # print(json.dumps(did, indent=2))
            add_to_dict(pers, "wind_speed", did["wind_speed"])

        timestamps.append(parse(t["time"]))
    df = pd.DataFrame(pers, index=timestamps)
    # print(df)
    df.index.name = "time"
    dfr = df.resample("30min").max().fillna(method="pad")
    now = datetime.datetime.now(tz=pytz.UTC)
    this_halfhour = now.replace(minute=0, second=0, microsecond=0)
    if (now - this_halfhour).total_seconds() > 30 * 60:
        this_halfhour += datetime.timedelta(minutes=30)
    last_halfhour = this_halfhour + datetime.timedelta(hours=8)
    # print(this_halfhour, last_halfhour)
    df_filtered: pd.DataFrame = dfr[(dfr.index >= this_halfhour) & (dfr.index < last_halfhour)]
    # print(df_filtered)
    return df_filtered


async def create_combined_forecast(lat: float, lon: float) -> pd.DataFrame:
    nowcast = await yrapiclient.get_nowcast(lat, lon)
    df_now = yr_precipitation_to_df(nowcast, "now")

    forecast = await yrapiclient.get_locationforecast(lat, lon)
    df_fore = yr_precipitation_to_df(forecast, "fore")

    merge = pd.concat([df_now, df_fore], axis=1)
    print(merge)
    assert len(merge.index) == 16
    return merge


async def create_output(lat: float, lon: float, format="bin", output=None):
    df = await create_combined_forecast(lat, lon)
    colors = []
    times = []
    for i in df.index:
        # Take always nowcast's precipitation, it should be the most accurate
        if pd.notnull(df["prec_now"][i]):
            precipitation = df["prec_now"][i]
        else:
            precipitation = df["prec_fore"][i]
        key = ""
        if precipitation >= 3.0:
            key = "VERYHEAVYRAIN"
            color = COLORS[key]
        elif precipitation >= 1.5:
            key = "HEAVYRAIN"
            color = COLORS[key]
        elif precipitation >= 0.5:
            key = "LIGHTRAIN"
            color = COLORS[key]
        elif precipitation > 0.0 and "rain" in df["symbol"][i]:
            key = "LIGHTRAIN"
            color = COLORS[key]
        elif precipitation == 0.0 and "rain" in df["symbol"][i]:
            key = "CLOUDY"
            color = COLORS[key]
        else:
            color = symbolmap[df["symbol"][i]]
        logging.debug("{} {} {} {} {}".format(
            precipitation, df["prec_now"][i], df["prec_fore"][i], df["symbol"][i], color)
        )
        colors += color + [0]  # Empty slot for future wind speed
        times.append([str(i), key, df["symbol"][i], color])
    assert len(colors) == 64
    arr = bytearray(colors)
    if output is not None:
        with open(output, "wb") as f:
            f.write(arr)
    if format == "json":
        return json.dumps(times, indent=2)
    elif format == "html":
        html = ["<html><head></head><body><table>\n"]
        for t in times:
            color = "rgb({})".format(",".join([str(x) for x in t[3]]))
            html.append(f"<tr><td style='background-color: {color}'>{t[0]} {t[1]} {t[2]}</td></tr>")
        html.append("</table></html>")
        return "\n".join(html)
    else:  # format == "bin":
        return arr


async def main(lat: float = 60.17, lon: float = 24.95):
    await create_output(lat, lon)


if __name__ == "__main__":
    import sys
    import asyncio

    logging.basicConfig(level=logging.DEBUG)

    if len(sys.argv) == 3:
        asyncio.run(main(float(sys.argv[1]), float(sys.argv[2])))
    else:
        asyncio.run(main())
