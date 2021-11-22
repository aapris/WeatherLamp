import datetime
import logging
import re
from typing import Union

import astral
import astral.sun
import pandas as pd
import pytz
from dateutil.parser import parse

# A dict to map weather symbol to particular RGB color

symbolmap = {
    **dict.fromkeys(
        [
            "clearsky",
            "fair",
        ],
        "CLEARSKY",
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


def yr_precipitation_to_df(yrdata: dict, cast: str, slot_minutes: int, slot_count: int, now=None) -> pd.DataFrame:
    timeseries = yrdata["properties"]["timeseries"]
    timestamps = []
    pers = {}
    for t in timeseries:
        if cast == "now":  # nowcast has only precipitation rate
            did = t["data"]["instant"]["details"]
            if "precipitation_rate" in did:  # radar data is available
                add_to_dict(pers, f"prec_{cast}", did["precipitation_rate"])
            else:
                logging.warning(f"Precipitation rate (radar data) is not available at {t['time']}: {did}")
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
            # add_to_dict(pers, "symbol_code", symbol_code)
            add_to_dict(pers, "symbol", symbol)
            # add_to_dict(pers, "variant", variant)
            # Wind and other forecasts
            did = t["data"]["instant"]["details"]
            add_to_dict(pers, "wind_speed", did["wind_speed"])
            add_to_dict(pers, "wind_gust", did["wind_speed_of_gust"])

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
    starttime, endttime = get_start_and_end(slot_minutes, slot_count, now)
    df_filtered: pd.DataFrame = dfr[(dfr.index >= starttime) & (dfr.index < endttime)]
    return df_filtered


def create_combined_forecast(nowcast: dict, forecast: dict, slot_minutes: int, slot_count: int,
                             now=None) -> pd.DataFrame:
    """
    Put nowcast and forecast into a Pandas DataFrame and merge the result.

    :param nowcast: nowcast data from YR API
    :param forecast: forecast data from YR API
    :param slot_minutes:
    :param slot_count:
    :return: DataFrame with precipitation forecast for next slot_count of slot_minutes 16
    """
    if nowcast is None:  # create mock nowcast, if it was None
        st, et = get_start_and_end(slot_minutes, slot_count)
        timestamps = [st + datetime.timedelta(minutes=x * slot_minutes) for x in list(range(0, slot_count))]
        pers = {}
        for _ in list(range(0, slot_count)):
            add_to_dict(pers, f"prec_now", None)
        df_now = pd.DataFrame(pers, index=timestamps)
        df_now.index.name = "time"
    else:
        df_now = yr_precipitation_to_df(nowcast, "now", slot_minutes, slot_count, now)

    df_fore = yr_precipitation_to_df(forecast, "fore", slot_minutes, slot_count, now)
    merge = pd.concat([df_now, df_fore], axis=1)
    # TODO: append missing rows instead of raising exception
    assert len(merge.index) == slot_count
    return merge


def add_day_night(df: pd.DataFrame, lat: float, lon: float):
    """
    Add day/night information to the DataFrame.

    :param df: DataFrame containing weather data
    :param lat: latitude
    :param lon: longitude
    :return: enhanced DataFrame
    """
    daynight = []
    loc = astral.LocationInfo("", "", "", lat, lon)
    for i in df.index:
        sun = astral.sun.sun(loc.observer, date=i)
        if sun["sunrise"] < i < sun["sunset"]:
            daynight.append(1)
        else:
            daynight.append(0)
    df["day"] = daynight
    return df


def add_symbol_and_color(df: pd.DataFrame, colormap: dict):
    """
    Color logic happens here. Use nowcast's precipitation, when it is available and
    otherwise forecast's weather symbol (defined by YR).

    :param df: DataFrame containing weather data
    :param colormap: color definitions to use
    :return: enhanced DataFrame
    """
    symbols = []
    colors = []
    rain_re = re.compile(r"rain|sleet|snow", re.IGNORECASE)
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
                symbols.append(colors_key)
                colors.append(colormap[colors_key])
            elif precipitation >= 1.5:
                colors_key = "HEAVYRAIN"
                symbols.append(colors_key)
                colors.append(colormap[colors_key])
            elif precipitation >= 0.5:
                colors_key = "RAIN"
                symbols.append(colors_key)
                colors.append(colormap[colors_key])
            elif precipitation > 0.0:
                colors_key = "LIGHTRAIN"
                symbols.append(colors_key)
                colors.append(colormap[colors_key])
            elif precipitation == 0.0 and rain_re.findall(df["symbol"][i]):
                colors_key = "CLOUDY"
                symbols.append(colors_key)
                colors.append(colormap[colors_key])
            else:
                colors_key = symbolmap[df["symbol"][i]]
                symbols.append(colors_key)
                colors.append(colormap[colors_key])
        else:
            symbol = df["symbol"][i]
            colors_key = symbolmap[symbol]
            if colors_key == "LIGHTRAIN":
                if prob_of_prec <= 50:
                    colors_key = "LIGHTRAIN_LT50"
            symbols.append(colors_key)
            colors.append(colormap[colors_key])
    df["wl_symbol"] = symbols
    df["color"] = colors
    return df
