import datetime
import logging
import re

import astral
import astral.sun
import pandas as pd

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


def add_to_dict(dict_: dict, key: str, val: float | None):
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
        now = datetime.datetime.now(datetime.UTC)
    start_time = now.replace(minute=0, second=0, microsecond=0)
    minutes_over = (now - start_time).total_seconds() / 60
    if minutes_over > slot_len:
        multiplier = minutes_over // slot_len
        start_time += datetime.timedelta(minutes=slot_len * multiplier)

    end_time = start_time + datetime.timedelta(minutes=slot_len * slot_count)
    return start_time, end_time


def _parse_yr_timeseries_to_df(yrdata: dict, cast: str) -> pd.DataFrame:
    """
    Parses YR timeseries data into a Pandas DataFrame without resampling or filtering.

    :param yrdata: Raw timeseries data from YR API.
    :param cast: Type of forecast ('now' or 'fore').
    :return: DataFrame containing the parsed timeseries data.
    """
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
                # Skip entries if next_1_hours is missing, could indicate end of relevant forecast
                logging.debug(f"Missing 'next_1_hours' data at {t['time']}")
                continue  # Skip this timestamp
            d1h = t["data"]["next_1_hours"]
            # Precipitation
            add_to_dict(pers, f"prec_{cast}", d1h["details"]["precipitation_amount"])
            add_to_dict(pers, "prob_of_prec", d1h["details"]["probability_of_precipitation"])
            # Weather symbol
            symbol_code = d1h["summary"]["symbol_code"]
            if symbol_code.find("_") >= 0:  # Check for _day, _night postfix
                symbol, _ = symbol_code.split("_")  # variant not used currently
            else:
                symbol = symbol_code
            add_to_dict(pers, "symbol", symbol)
            # Wind and other forecasts
            did = t["data"]["instant"]["details"]
            add_to_dict(pers, "wind_speed", did["wind_speed"])
            add_to_dict(pers, "wind_gust", did["wind_speed_of_gust"])

        timestamps.append(datetime.datetime.fromisoformat(t["time"]))

    # Ensure all lists in pers have the same length as timestamps
    # This handles cases where some data points might be skipped (like missing next_1_hours)
    expected_len = len(timestamps)
    for key in list(pers.keys()):  # Iterate over a copy of keys
        if len(pers[key]) != expected_len:
            logging.error(
                f"Inconsistent data length for key '{key}' in cast '{cast}'. Expected {expected_len}, got {len(pers[key])}. Removing key."
            )
            del pers[key]  # Remove inconsistent data

    if not pers:  # If no valid data was parsed
        logging.warning(f"No valid data parsed for cast '{cast}'. Returning empty DataFrame.")
        return pd.DataFrame(index=pd.to_datetime(timestamps)).rename_axis("time")

    df = pd.DataFrame(pers, index=pd.to_datetime(timestamps))
    df.index.name = "time"
    print("cast", cast, "df", df)
    return df


def yr_precipitation_to_df(yrdata: dict, cast: str) -> pd.DataFrame:
    """
    Parses YR API data (nowcast or forecast) into a full Pandas DataFrame.
    This function returns the complete timeseries data without resampling or time filtering.

    :param yrdata: Raw data dictionary from YR API.
    :param cast: Type of data ('now' for nowcast, 'fore' for forecast).
    :return: Pandas DataFrame with the full timeseries data.
    """
    return _parse_yr_timeseries_to_df(yrdata, cast)


def create_combined_forecast(
    nowcast: dict | None, forecast: dict, slot_minutes: int, slot_count: int, now=None
) -> pd.DataFrame:
    """
    Fetches nowcast and forecast data, resamples and filters them to the specified slots,
    and merges the results into a single Pandas DataFrame.

    :param nowcast: Raw nowcast data from YR API (or None).
    :param forecast: Raw forecast data from YR API.
    :param slot_minutes: Length of each time slot in minutes.
    :param slot_count: Number of time slots to include.
    :param now: The reference time for calculating slots (defaults to current time).
    :return: DataFrame with combined, resampled, and filtered precipitation forecast.
    """
    starttime, endttime = get_start_and_end(slot_minutes, slot_count, now)
    res_min = f"{slot_minutes}min"

    if nowcast is None:  # create mock nowcast DataFrame if nowcast data is None
        timestamps = pd.date_range(start=starttime, periods=slot_count, freq=res_min, tz=datetime.UTC)
        df_now_resampled = pd.DataFrame({"prec_now": [None] * slot_count}, index=timestamps)
        df_now_resampled.index.name = "time"
    else:
        df_now_full = yr_precipitation_to_df(nowcast, "now")
        if not df_now_full.empty:
            # Resample nowcast: aggregate and forward fill
            dfr_now = (
                df_now_full.resample(res_min)
                .agg(
                    {
                        "prec_now": ["min", "max", "mean"]  # Apply aggregations only to prec_now
                    }
                )
                .ffill()
            )
            # Flatten multi-index columns if they exist (e.g., ('prec_now', 'max'))
            if isinstance(dfr_now.columns, pd.MultiIndex):
                dfr_now.columns = [
                    "_".join(col).strip() if isinstance(col, tuple) else col for col in dfr_now.columns.values
                ]
                # Rename for clarity and select the desired column
                dfr_now = dfr_now.rename(columns={"prec_now_max": "prec_now"})
                df_now_resampled = dfr_now[["prec_now"]]  # Select only the final prec_now column
            else:  # Handle case where columns might not be MultiIndex after agg if only one agg func/column
                df_now_resampled = dfr_now
            # Filter by time
            df_now_resampled = df_now_resampled[
                (df_now_resampled.index >= starttime) & (df_now_resampled.index < endttime)
            ]
        else:  # Handle empty dataframe after parsing
            timestamps = pd.date_range(start=starttime, periods=slot_count, freq=res_min, tz=datetime.UTC)
            df_now_resampled = pd.DataFrame({"prec_now": [None] * slot_count}, index=timestamps)
            df_now_resampled.index.name = "time"

    df_fore_full = yr_precipitation_to_df(forecast, "fore")
    if not df_fore_full.empty:
        # Resample forecast: max and forward fill
        # Select only columns relevant for forecast resampling before resampling
        cols_to_resample = ["prec_fore", "prob_of_prec", "symbol", "wind_speed", "wind_gust"]
        # Ensure all expected columns exist, add missing ones with NaN if necessary
        for col in cols_to_resample:
            if col not in df_fore_full.columns:
                df_fore_full[col] = None  # Add missing column with Nones or pd.NA

        # Resample using max for numeric types, first for symbol (or last, max might error on strings)
        # Using a dictionary for agg specifies how to handle each column
        agg_funcs = {
            col: "max" for col in df_fore_full.select_dtypes(include="number").columns if col in cols_to_resample
        }
        if "symbol" in cols_to_resample and "symbol" in df_fore_full.columns:
            agg_funcs["symbol"] = "first"  # Use 'first' occurrence for symbol in the interval

        dfr_fore = df_fore_full[list(agg_funcs.keys())].resample(res_min).agg(agg_funcs).ffill()

        # Filter by time
        df_fore_resampled = dfr_fore[(dfr_fore.index >= starttime) & (dfr_fore.index < endttime)]
    else:  # Handle empty dataframe after parsing
        timestamps = pd.date_range(start=starttime, periods=slot_count, freq=res_min, tz=datetime.UTC)
        # Create empty df with expected columns
        cols = ["prec_fore", "prob_of_prec", "symbol", "wind_speed", "wind_gust"]
        df_fore_resampled = pd.DataFrame(index=timestamps, columns=cols)
        df_fore_resampled.index.name = "time"

    # Merge the resampled and filtered dataframes
    merge = pd.concat([df_now_resampled, df_fore_resampled], axis=1)

    # Ensure the final merged frame has exactly slot_count rows, pad if necessary
    expected_index = pd.date_range(start=starttime, periods=slot_count, freq=res_min, tz=datetime.UTC)
    merge = merge.reindex(expected_index).ffill()

    # The assertion might fail if resampling/filtering doesn't produce exactly slot_count rows
    # It's safer to ensure the length after reindexing/padding
    # assert len(merge.index) == slot_count, f"Merged DataFrame length {len(merge.index)} != slot_count {slot_count}"
    if len(merge.index) != slot_count:
        logging.warning(
            f"Merged DataFrame length {len(merge.index)} != slot_count {slot_count}. Final index range: {merge.index.min()} to {merge.index.max()}"
        )
        # Ensure the final DataFrame has the correct shape, even if padding was needed.
        merge = merge.head(slot_count)  # Or handle error appropriately

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
