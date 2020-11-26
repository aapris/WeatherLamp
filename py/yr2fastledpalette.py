import sys
import datetime
import pandas as pd
import re
import requests
from io import StringIO
import os
import json
import argparse
import logging
import time
import pytz
from dateutil.parser import parse
from itertools import cycle


api_url = "https://api.met.no/weatherapi/locationforecast/2.0/complete"
user_agent = "WeatherLamp/0.1 github.com/aapris/WeatherLamp"


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

# print(json.dumps(myDict, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument(
        "--log",
        dest="log",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="ERROR",
        help="Set the logging level",
    )
    parser.add_argument(
        "--lat", required=True, help="Latitude in decimal format (dd.ddd)"
    )
    parser.add_argument(
        "--lon", required=True, help="Longitude in decimal format (dd.ddd)"
    )
    parser.add_argument("--output", help="Output file name")
    args = parser.parse_args()
    if args.log:
        logging.basicConfig(
            level=getattr(logging, args.log),
            datefmt="%Y-%m-%dT%H:%M:%S",
            format="%(asctime)s.%(msecs)03dZ %(levelname)s %(message)s",
        )
        logging.Formatter.converter = time.gmtime  # Timestamps in UTC time
    return args


def get_yrdata(args):
    cachefile = f"yr-cache-{args.lat}_{args.lon}.json"
    if os.path.isfile(cachefile):
        logging.info(f"Using cached data from {cachefile}")
        with open(cachefile, "rt") as f:
            yrdata = json.loads(f.read())
    else:
        parameters = f"lat={args.lat}&lon={args.lon}"
        headers = {"User-Agent": user_agent}
        full_url = f"{api_url}?{parameters}"
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
        print(res.headers)
    return yrdata


def parse_yrdata(args, yrdata):
    this_hour = datetime.datetime.now(tz=pytz.UTC).replace(
        minute=0, second=0, microsecond=0
    )
    tseries = yrdata["properties"]["timeseries"]
    fore = []
    for t in tseries:
        ts = parse(t["time"])
        if this_hour > ts:
            continue
        fore.append(t)
        if len(fore) == 8:
            break
    colors = []
    for t in fore:
        if "next_1_hours" not in t["data"]:
            break
        symbol_code = t["data"]["next_1_hours"]["summary"]["symbol_code"]
        if symbol_code.find("_") >= 0:  # Check for _day, _night postfix
            symbol, variant = symbol_code.split("_")
        else:
            symbol, variant = symbol_code, None
        # Handle very heavy rain
        prec = t["data"]["next_1_hours"]["details"]["precipitation_amount"]
        if prec >= 3.0:
            color = COLOUR_VERYHEAVYRAIN
        else:
            color = symbolmap[symbol]
        # Handle light rain with probability
        prob_prec = t["data"]["next_1_hours"]["details"]["probability_of_precipitation"]
        if symbol == 'lightrain' and prob_prec >= 70:
            color = COLOUR_LIGHTRAIN_GT50
        colorwind = color + [
            int(t["data"]["instant"]["details"]["wind_speed"] / 5)
        ]
        colors += colorwind * 2  # Add colors twice because we have 16 slots
        print('{} {} {} {}% {}mm {} {}m/s'.format(
            t["time"],
            symbol,
            variant,
            prob_prec,
            prec,
            colorwind,
            t["data"]["instant"]["details"]["wind_speed"],
        ))

    if args.output is not None:
        arr = bytearray(colors)
        with open(args.output, "wb") as f:
            f.write(arr)

    # with open(sys.argv[1] + ".json", "wt") as f:
    #     f.write(json.dumps(readable_colors, indent=2))


def main():
    args = parse_args()
    yrdata = get_yrdata(args)
    parse_yrdata(args, yrdata)


if __name__ == "__main__":
    main()
