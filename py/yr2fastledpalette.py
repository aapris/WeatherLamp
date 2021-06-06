import argparse
import datetime
import json
import logging
import os
import time

import pandas as pd
import pytz
import requests
from dateutil.parser import parse

API_URL: str = "https://api.met.no/weatherapi/{}/2.0/complete"
USER_AGENT: str = "WeatherLamp/0.2 github.com/aapris/WeatherLamp"

# TODO: these should be in some configuration file
COLOUR_CLEARSKY_NIGHT = [5, 18, 151]
COLOUR_CLEARSKY_DAY = [20, 108, 214]
COLOUR_PARTLYCLOUDY = [40, 158, 154]
COLOUR_CLOUDY = [70, 200, 140]
COLOUR_LIGHTRAIN = [90, 200, 1]
COLOUR_LIGHTRAIN_GT50 = [110, 180, 1]
COLOUR_RAIN = [202, 252, 1]
COLOUR_HEAVYRAIN = [173, 133, 2]
COLOUR_VERYHEAVYRAIN = [143, 93, 2]

symbolmap = {
    **dict.fromkeys(
        [
            "clearsky",
            "fair",
        ],
        COLOUR_CLEARSKY_DAY,
    ),
    **dict.fromkeys(
        [
            "partlycloudy",
        ],
        COLOUR_PARTLYCLOUDY,
    ),
    **dict.fromkeys(
        [
            "cloudy",
            "fog",
        ],
        COLOUR_CLOUDY,
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
        COLOUR_HEAVYRAIN,
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
        COLOUR_LIGHTRAIN,
    ),
    **dict.fromkeys(
        [
            "rain",
            "rainandthunder",
            "rainshowers",
            "rainshowersandthunder",
        ],
        COLOUR_RAIN,
    ),
    **dict.fromkeys(
        [
            "sleet",
            "sleetandthunder",
            "sleetshowers",
            "sleetshowersandthunder",
        ],
        COLOUR_RAIN,
    ),
    **dict.fromkeys(
        [
            "snow",
            "snowandthunder",
            "snowshowers",
            "snowshowersandthunder",
        ],
        COLOUR_RAIN,
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


def get_yrdata(args, cast_type="locationforecast"):
    cachefile = f"yr-cache-{cast_type}.{args.lat}_{args.lon}.json"
    if os.path.isfile(cachefile):
        logging.info(f"Using cached data from {cachefile}")
        with open(cachefile, "rt") as f:
            yrdata = json.loads(f.read())
    else:
        parameters = f"lat={args.lat}&lon={args.lon}"
        headers = {"User-Agent": USER_AGENT}
        url = API_URL.format(cast_type)
        full_url = f"{url}?{parameters}"
        logging.info(f"Requesting data from {full_url}")
        res = requests.get(full_url, headers=headers)
        if res.status_code == 200:
            logging.info(f"Got 200 OK")
        elif res.status_code == 203:
            logging.warning(f"Got 203, read the docs")
        else:
            logging.warning(f"Got {res.status_code}!")
        logging.info(f"Caching data to {cachefile}")
        yrdata = res.json()
        with open(cachefile, "wt") as f:
            f.write(json.dumps(yrdata, indent=2))
        logging.debug(res.headers)
    return yrdata


def add_to_dict(dict_: dict, key: str, val: float):
    if key not in dict_:
        dict_[key] = []
    dict_[key].append(val)


def yr_precipitation_to_df(args, yrdata, cast):
    timeseries = yrdata["properties"]["timeseries"]
    tss = []
    pers = {}
    for t in timeseries:
        if cast == "now":  # nowcast has only precipitation rate
            did = t["data"]["instant"]["details"]
            add_to_dict(pers, f"precipitation_{cast}", did["precipitation_rate"])
        elif cast == "fore":  # forecast has more data available
            if "next_1_hours" not in t["data"]:
                break
            d1h = t["data"]["next_1_hours"]
            # Precipitation
            add_to_dict(pers, f"precipitation_{cast}", d1h["details"]["precipitation_amount"])
            add_to_dict(pers, "probability_of_precipitation", d1h["details"]["probability_of_precipitation"])
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

        tss.append(parse(t["time"]))
    df = pd.DataFrame(pers, index=tss)
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
    print(df_filtered)
    return df_filtered


def create_combined_forecast(args: argparse.Namespace) -> pd.DataFrame:
    nowcast = get_yrdata(args, "nowcast")
    df_now = yr_precipitation_to_df(args, nowcast, "now")

    forecast = get_yrdata(args)
    df_fore = yr_precipitation_to_df(args, forecast, "fore")

    merge = pd.concat([df_now, df_fore], axis=1)
    print(merge)
    assert len(merge.index) == 16
    return merge


def create_output(args: argparse.Namespace):
    df = create_combined_forecast(args)
    colors = []
    for i in df.index:
        # Take greater value of precipitations
        if df["precipitation_now"][i] > df["precipitation_fore"][i]:
            precipitation = df["precipitation_now"][i]
        else:
            precipitation = df["precipitation_fore"][i]
        if precipitation >= 3.0:
            color = COLOUR_VERYHEAVYRAIN
        elif precipitation >= 0.5:
            color = COLOUR_LIGHTRAIN
        else:
            color = symbolmap[df["symbol"][i]]

        # print(df['precipitation_now'][i], df['precipitation_fore'][i], precipitation)
        colors += color + [0]
    assert len(colors) == 64
    if args.output is not None:
        arr = bytearray(colors)
        with open(args.output, "wb") as f:
            f.write(arr)


def main():
    args = parse_args()
    create_output(args)


if __name__ == "__main__":
    main()
