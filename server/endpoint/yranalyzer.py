import argparse
import datetime
import json
import logging
import time
from collections import OrderedDict
from typing import Union

import astral
import astral.sun
import pandas as pd
import pytz
from dateutil.parser import parse

# PACKAGE_PARENT = '..'
# SCRIPT_DIR = os.path.dirname(os.path.realpath(os.path.join(os.getcwd(), os.path.expanduser(__file__))))
# sys.path.append(os.path.normpath(os.path.join(SCRIPT_DIR, PACKAGE_PARENT)))
# print(sys.path)
import yrapiclient

# TODO: these should be in some configuration file

COLORMAPS = OrderedDict()

COLORMAPS["plain"] = {
    "CLEARSKY_DAY": [3, 3, 235],
    "PARTLYCLOUDY": [65, 126, 205],
    "CLOUDY": [180, 200, 200],
    "LIGHTRAIN_LT50": [161, 228, 74],
    "LIGHTRAIN": [240, 240, 42],
    "RAIN": [241, 155, 44],
    "HEAVYRAIN": [236, 94, 42],
    "VERYHEAVYRAIN": [234, 57, 248],
}

COLORMAPS["plywood"] = {
    "CLEARSKY_DAY": [20, 108, 214],
    "PARTLYCLOUDY": [40, 158, 154],
    "CLOUDY": [70, 200, 140],
    "LIGHTRAIN_LT50": [110, 180, 1],
    "LIGHTRAIN": [90, 200, 1],
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
        "CLEARSKY_DAY",
    ),
    **dict.fromkeys(
        [
            "partlycloudy",
        ],
        "PARTLYCLOUDY",
    ),
    **dict.fromkeys(
        [
            "cloudy",
            "fog",
        ],
        "CLOUDY",
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
        "HEAVYRAIN",
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
        "LIGHTRAIN",
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
        "RAIN",
    ),
}


def parse_args() -> argparse.Namespace:
    """
    Parse command line arguments and set up logging level
    :return: parsed args
    """
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


def add_to_dict(dict_: dict, key: str, val: Union[float, None]):
    """
    Helper function to add key/value pairs to a dict, where value is a list of floats.

    :param dict_: target dict
    :param key: key name
    :param val: value
    """
    if key not in dict_:
        dict_[key] = []
    dict_[key].append(val)


def get_start_and_end(slot_len: int, slot_count: int, now=None):
    """
    Calculate start and end times for given time slot length, slot count and timestamp.
    E.g. 15, 16, 2021-09-14 14:55:52 returns
    2021-09-14 14:45:00 2021-09-14 18:45:00

    :param slot_len:
    :param slot_count:
    :param now:
    :return:
    """
    if now is None:
        now = pytz.utc.localize(datetime.datetime.utcnow())
    start_time = now.replace(minute=0, second=0, microsecond=0)
    minutes_over = (now - start_time).total_seconds() / 60
    if minutes_over > slot_len:
        multiplier = minutes_over // slot_len
        start_time += datetime.timedelta(minutes=slot_len * multiplier)

    end_time = start_time + datetime.timedelta(minutes=slot_len * slot_count)
    return start_time, end_time


def yr_precipitation_to_df(yrdata: dict, cast: str, slot_minutes: int, slot_count: int) -> pd.DataFrame:
    timeseries = yrdata["properties"]["timeseries"]
    timestamps = []
    pers = {}
    for t in timeseries:
        if cast == "now":  # nowcast has only precipitation rate
            did = t["data"]["instant"]["details"]
            if "precipitation_rate" in did:  #  radar data is available
                add_to_dict(pers, f"prec_{cast}", did["precipitation_rate"])
            else:
                logging.warning(f"Precipitation rate (radar data) is not available: {did}")
                add_to_dict(pers, f"prec_{cast}", None)
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
            add_to_dict(pers, "wind_speed", did["wind_speed"])

        timestamps.append(parse(t["time"]))
    df = pd.DataFrame(pers, index=timestamps)
    df.index.name = "time"
    res_min = f"{slot_minutes}min"
    if cast == "now":  # nowcast has only precipitation rate
        dfr = df.resample(res_min).agg(['min', 'max', 'mean']).fillna(method="pad")
        # Remove prec_now level from column title and keep only one [min, max, mean]
        dfr.columns = dfr.columns.droplevel(0)
        dfr["prec_now"] = dfr["max"]  # Use max value for precipitation
    else:  # cast == "fore":  # forecast has more data available
        dfr = df.resample(res_min).max().fillna(method="pad")
    # Filter out just requested number of data rows
    starttime, endttime = get_start_and_end(slot_minutes, slot_count)
    df_filtered: pd.DataFrame = dfr[(dfr.index >= starttime) & (dfr.index < endttime)]
    return df_filtered


async def create_combined_forecast(lat: float, lon: float, slot_minutes: int, slot_count: int,
                                   dev: bool = False) -> pd.DataFrame:
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
    if nowcast is None:  # create mock nowcast, if it was not found
        st, et = get_start_and_end(slot_minutes, slot_count)
        # print(st, et)
        timestamps = [st + datetime.timedelta(minutes=x * slot_minutes) for x in list(range(0, slot_count))]
        pers = {}
        for x in list(range(0, slot_count)):
            add_to_dict(pers, f"prec_now", None)
        df_now = pd.DataFrame(pers, index=timestamps)
        df_now.index.name = "time"
    else:
        df_now = yr_precipitation_to_df(nowcast, "now", slot_minutes, slot_count)

    forecast = await yrapiclient.get_locationforecast(lat, lon, dev)
    df_fore = yr_precipitation_to_df(forecast, "fore", slot_minutes, slot_count)

    merge = pd.concat([df_now, df_fore], axis=1)
    # Add day/night information to the DataFrame
    # loc = astral.LocationInfo("", "", "", lat, lon)
    # for i in range(0, len(merge.index)):
    #     sun = astral.sun.sun(loc.observer, date=merge.index[i])
    #     print(sun["sunrise"], sun["sunset"])
    print(merge)

    # TODO: append missing rows instead of raising exception
    print(len(merge.index), slot_count)
    assert len(merge.index) == slot_count
    return merge


async def create_output(
        lat: float, lon: float, _format: str = "bin",
        slot_minutes: int = 30, slot_count: int = 16,
        colormap_name: str = "plain",
        output: str = None,
        dev: bool = False) -> Union[str, bytearray]:
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
    df = await create_combined_forecast(lat, lon, slot_minutes, slot_count, dev)
    colors = []
    times = []
    if colormap_name in COLORMAPS:
        colormap = COLORMAPS[colormap_name]
    else:
        colormap = COLORMAPS[list(COLORMAPS.keys())[0]]
    cnt = 0
    for i in df.index:
        # Take always nowcast's precipitation, it should be the most accurate
        if pd.notnull(df["prec_now"][i]):
            precipitation = df["prec_now"][i]
            nowcast = True
        else:
            precipitation = df["prec_fore"][i]
            nowcast = False
        prob_of_prec = df["prob_of_prec"][i]
        if nowcast:
            if precipitation >= 3.0:
                colors_key = "VERYHEAVYRAIN"
                color = colormap[colors_key]
            elif precipitation >= 1.5:
                colors_key = "HEAVYRAIN"
                color = colormap[colors_key]
            elif precipitation >= 0.5:
                colors_key = "RAIN"
                color = colormap[colors_key]
            elif precipitation > 0.0:
                colors_key = "LIGHTRAIN"
                color = colormap[colors_key]
            elif precipitation == 0.0 and "rain" in df["symbol"][i]:
                colors_key = "CLOUDY"
                color = colormap[colors_key]
            else:
                colors_key = symbolmap[df["symbol"][i]]
                color = colormap[colors_key]
        else:
            symbol = df["symbol"][i]
            colors_key = symbolmap[symbol]
            if colors_key == "LIGHTRAIN":
                if prob_of_prec <= 50:
                    colors_key = "LIGHTRAIN_LT50"
            color = colormap[colors_key]
        # Temporary kludge to test all color definitions
        # if True:
        #     size = slot_count // len(colormap.keys()) + 1
        #     c_idx = cnt // size
        #     colorss = list(colormap.values())
        #     color = colorss[c_idx]
        logging.debug("{} {} {} {} {} {} {}".format(
            precipitation, df["prec_now"][i], df["prec_fore"][i], prob_of_prec, df["symbol"][i], colors_key, color)
        )
        colors += color + [0]  # Empty slot for future wind speed
        times.append({
            "time": str(i),
            "color_key": colors_key,
            "yr_symbol": df["symbol"][i],
            "prec_nowcast": df["prec_now"][i],
            "prec_forecast": df["prec_fore"][i],
            "prob_of_prec": prob_of_prec,
            "rgb": color
        })
        cnt += 1
    assert len(colors) == slot_count * 4
    arr = bytearray(colors)
    if output is not None:
        with open(output, "wb") as f:
            f.write(arr)
    if _format == "json":
        return json.dumps(times, indent=2)
    elif _format == "html":
        html = ["""<html><head>
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
        </head><body><table class="container">\n"""]
        html.append("""<tr>
        <td>time</td>
        <td>yr_symbol</td>
        <td>color_key</td>
        <td>prec</td>
        <td></td>
        </tr>""")
        for t in times:
            t["formatted_color"] = "rgb({})".format(",".join([str(x) for x in t["rgb"]]))
            html.append("""<tr style='background-color: {formatted_color}'>
            <td>{time}</td>
            <td>{yr_symbol}</td>
            <td>{color_key}</td>
            <td>{prec_nowcast}/{prec_forecast}</td>
            <td></td>
            </tr>""".format(**t))
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
