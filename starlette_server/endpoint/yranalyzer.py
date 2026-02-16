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


def _extract_symbol_from_code(symbol_code: str) -> str:
    """
    Extracts the base symbol from a symbol code by removing _day/_night postfix.

    :param symbol_code: Symbol code from YR API (e.g., 'clearsky_day').
    :return: Base symbol without postfix (e.g., 'clearsky').
    """
    if symbol_code.find("_") >= 0:
        symbol, _ = symbol_code.split("_")
        return symbol
    return symbol_code


def _parse_nowcast_entry(t: dict, pers: dict, timestamps: list, cast: str):
    """
    Parses a single nowcast entry and adds it to the data structures.

    :param t: Single timeseries entry from YR API.
    :param pers: Dictionary to store parsed data.
    :param timestamps: List to store timestamps.
    :param cast: Type of forecast ('now').
    """
    did = t["data"]["instant"]["details"]
    if "precipitation_rate" in did:
        add_to_dict(pers, f"prec_{cast}", did["precipitation_rate"])
    else:
        logging.warning(f"Precipitation rate (radar data) is not available at {t['time']}: {did}")
        add_to_dict(pers, f"prec_{cast}", None)
    timestamps.append(datetime.datetime.fromisoformat(t["time"]))


def _parse_forecast_1h_entry(t: dict, pers: dict, timestamps: list, cast: str):
    """
    Parses a single hourly forecast entry and adds it to the data structures.

    :param t: Single timeseries entry from YR API.
    :param pers: Dictionary to store parsed data.
    :param timestamps: List to store timestamps.
    :param cast: Type of forecast ('fore').
    """
    base_time = datetime.datetime.fromisoformat(t["time"])
    d1h = t["data"]["next_1_hours"]
    did = t["data"]["instant"]["details"]

    # Precipitation
    add_to_dict(pers, f"prec_{cast}", d1h["details"]["precipitation_amount"])
    # probability_of_precipitation is not always present, at least outside of Scandinavia
    add_to_dict(pers, "prob_of_prec", d1h["details"].get("probability_of_precipitation"))
    # Weather symbol
    symbol = _extract_symbol_from_code(d1h["summary"]["symbol_code"])
    add_to_dict(pers, "symbol", symbol)
    # Wind and other forecasts
    add_to_dict(pers, "wind_speed", did["wind_speed"])
    add_to_dict(pers, "wind_gust", did.get("wind_speed_of_gust"))
    timestamps.append(base_time)


def _parse_forecast_6h_entry(t: dict, pers: dict, timestamps: list, cast: str):
    """
    Parses a single 6-hour forecast entry and expands it to hourly intervals.

    :param t: Single timeseries entry from YR API.
    :param pers: Dictionary to store parsed data.
    :param timestamps: List to store timestamps.
    :param cast: Type of forecast ('fore').
    """
    base_time = datetime.datetime.fromisoformat(t["time"])
    d6h = t["data"]["next_6_hours"]
    did = t["data"]["instant"]["details"]

    # Get 6-hour data and divide by 6 for hourly average
    precip_6h = d6h["details"].get("precipitation_amount", 0.0)
    precip_hourly = precip_6h / 6.0 if precip_6h is not None else 0.0

    prob_of_prec_6h = d6h["details"].get("probability_of_precipitation")

    # Weather symbol from 6-hour forecast
    symbol = _extract_symbol_from_code(d6h["summary"]["symbol_code"])

    # Create 6 hourly entries
    for hour_offset in range(6):
        hour_time = base_time + datetime.timedelta(hours=hour_offset)
        timestamps.append(hour_time)
        add_to_dict(pers, f"prec_{cast}", precip_hourly)
        add_to_dict(pers, "prob_of_prec", prob_of_prec_6h)
        add_to_dict(pers, "symbol", symbol)
        add_to_dict(pers, "wind_speed", did.get("wind_speed"))
        add_to_dict(pers, "wind_gust", did.get("wind_speed_of_gust"))


def _parse_yr_timeseries_to_df(yrdata: dict | None, cast: str) -> pd.DataFrame:
    """
    Parses YR timeseries data into a Pandas DataFrame without resampling or filtering.
    For forecast data, uses next_1_hours when available, and expands next_6_hours data
    to hourly intervals when next_1_hours is not available.

    Returns an empty DataFrame if yrdata is None (defense-in-depth for API failures).

    :param yrdata: Raw timeseries data from YR API, or None.
    :param cast: Type of forecast ('now' or 'fore').
    :return: DataFrame containing the parsed timeseries data.
    """
    if yrdata is None:
        logging.warning(f"Received None yrdata for cast '{cast}'. Returning empty DataFrame.")
        return pd.DataFrame().rename_axis("time")

    timeseries = yrdata["properties"]["timeseries"]
    timestamps = []
    pers = {}

    for t in timeseries:
        if cast == "now":
            _parse_nowcast_entry(t, pers, timestamps, cast)
        elif cast == "fore":
            if "next_1_hours" in t["data"]:
                _parse_forecast_1h_entry(t, pers, timestamps, cast)
            elif "next_6_hours" in t["data"]:
                _parse_forecast_6h_entry(t, pers, timestamps, cast)
            else:
                logging.debug(f"Missing both 'next_1_hours' and 'next_6_hours' data at {t['time']}")
                continue

    # Ensure all lists in pers have the same length as timestamps
    expected_len = len(timestamps)
    for key in list(pers.keys()):
        if len(pers[key]) != expected_len:
            logging.error(
                f"Inconsistent data length for key '{key}' in cast '{cast}'. Expected {expected_len}, got {len(pers[key])}. Removing key."
            )
            del pers[key]

    if not pers:
        logging.warning(f"No valid data parsed for cast '{cast}'. Returning empty DataFrame.")
        return pd.DataFrame(index=pd.to_datetime(timestamps)).rename_axis("time")

    df = pd.DataFrame(pers, index=pd.to_datetime(timestamps))
    df.index.name = "time"
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
    nowcast: dict | None, forecast: dict | None, slot_minutes: int, slot_count: int, now=None
) -> pd.DataFrame:
    """
    Fetches nowcast and forecast data, resamples and filters them to the specified slots,
    and merges the results into a single Pandas DataFrame.

    nowcast may be None, if original coordinates are not in YR coverage (yrapiclient.NOWCAST_COVERAGE_WKT),
    in which case a mock nowcast DataFrame is created.

    forecast may be None if the API is down and no cached data is available,
    in which case an empty placeholder DataFrame is created.

    :param nowcast: Raw nowcast data from YR API (or None).
    :param forecast: Raw forecast data from YR API (or None).
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
            dfr_now = df_now_full.resample(res_min, origin=starttime).agg(
                {
                    "prec_now": ["min", "max", "mean"]  # Apply aggregations only to prec_now
                }
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
        dfr_fore = df_fore_full[list(agg_funcs.keys())].resample(res_min, origin=starttime).agg(agg_funcs).ffill()
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
    # never use ffill, prec_nowcast is used when it is available and prec_forecast is used otherwise
    merge = merge.reindex(expected_index)  # .ffill()

    # The assertion might fail if resampling/filtering doesn't produce exactly slot_count rows
    # It's safer to ensure the length after reindexing/padding
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


def _determine_nowcast_color_key(precipitation: float, symbol, rain_re) -> str | None:
    """
    Determines the color key based on nowcast precipitation data.

    :param precipitation: Precipitation amount from nowcast.
    :param symbol: Weather symbol (may be None).
    :param rain_re: Compiled regex pattern for rain detection.
    :return: Color key string or None.
    """
    # Check precipitation thresholds using a threshold list
    thresholds = [(3.0, "VERYHEAVYRAIN"), (1.5, "HEAVYRAIN"), (0.5, "RAIN"), (0.0, "LIGHTRAIN")]
    for threshold, key in thresholds:
        if precipitation > threshold:
            return key

    # Check for zero precipitation - rain symbol takes precedence
    if precipitation == 0.0 and symbol is not None:
        if rain_re.findall(str(symbol)):
            return "CLOUDY"
        return symbolmap.get(symbol)

    return None


def _determine_forecast_color_key(symbol, prob_of_prec: float | None) -> str | None:
    """
    Determines the color key based on forecast symbol and probability.

    :param symbol: Weather symbol from forecast (may be None).
    :param prob_of_prec: Probability of precipitation (may be None).
    :return: Color key string or None.
    """
    if symbol is None or symbol not in symbolmap:
        return None

    colors_key = symbolmap[symbol]
    # Adjust for low probability light rain only if prob_of_prec is a valid number
    if colors_key == "LIGHTRAIN" and isinstance(prob_of_prec, (int, float)) and prob_of_prec <= 50:
        return "LIGHTRAIN_LT50"
    return colors_key


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
            symbol = df["symbol"][i] if "symbol" in df.columns and pd.notnull(df["symbol"][i]) else None
            colors_key = _determine_nowcast_color_key(precipitation, symbol, rain_re)
        else:
            precipitation = df["prec_fore"][i]
            prob_of_prec = (
                df["prob_of_prec"][i] if "prob_of_prec" in df.columns and pd.notnull(df["prob_of_prec"][i]) else None
            )
            symbol = df["symbol"][i] if "symbol" in df.columns and pd.notnull(df["symbol"][i]) else None
            colors_key = _determine_forecast_color_key(symbol, prob_of_prec)

        # Append symbol and color if colors_key was determined and exists in colormap
        if colors_key and colors_key in colormap:
            symbols.append(colors_key)
            colors.append(colormap[colors_key])
        else:
            # Append default/fallback values if colors_key is None or not in colormap
            logging.warning(
                f"Could not determine color for timestamp {i}. colors_key: {colors_key}. Appending default."
            )
            symbols.append("UNKNOWN")
            default_color = colormap.get("UNKNOWN", colormap.get("CLOUDY", [0, 0, 0]))
            colors.append(default_color)

    df["wl_symbol"] = symbols
    df["color"] = colors
    return df
