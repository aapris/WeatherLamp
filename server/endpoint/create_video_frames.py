import argparse
import datetime
import json
import logging
import time
from pathlib import Path
from typing import List, Tuple

import dateutil.parser
import pandas as pd
import pytz
from PIL import Image, ImageDraw, ImageFont

import yranalyzer

colormap = {
    "CLEARSKY": [3, 3, 235],
    "PARTLYCLOUDY": [65, 126, 205],
    "CLOUDY": [180, 200, 200],
    "LIGHTRAIN_LT50": [161, 228, 74],
    "LIGHTRAIN": [240, 240, 42],
    "RAIN": [241, 155, 44],
    "HEAVYRAIN": [236, 94, 42],
    "VERYHEAVYRAIN": [234, 57, 248],
}


def loop_casts(casts: list) -> dict:
    """Loop all casts and remove duplicates by putting them into a dict with first timestamp in timeseries list

    :param casts: list of paths
    :return: dict of casts as dict
    """
    by_start_time = dict()
    for now_c in casts:
        with open(now_c) as f:
            cast = json.load(f)
            start_time = cast["properties"]["timeseries"][0]["time"]
            by_start_time[start_time] = cast
    return by_start_time


def find_cast(by_start_time, ts):
    # Loop all timestamps and return last one which is smaller than ts
    tstamps = list(by_start_time.keys())
    prev_ts = tstamps.pop(0)
    for t in tstamps:
        if ts < t:
            return prev_ts
        else:
            prev_ts = t


def get_cast_files(directory: Path, lat: str, lon: str) -> Tuple[list, list]:
    entries = []
    # Save only files having lat and lon in the name
    for e in directory.iterdir():
        if lat in e.stem and lon in e.stem:
            entries.append(e)
    entries.sort()
    # Split the list to forecasts and nowcasts
    nowcasts = [str(x) for x in entries if "nowcast" in str(x)]
    locationforecasts = [str(x) for x in entries if "locationforecast" in str(x)]
    return nowcasts, locationforecasts


def get_tb_files(directory: Path) -> List[Path]:
    entries = list(directory.iterdir())
    entries.sort()
    return entries


def create_df(nowcasts_by_start_time, forecasts_by_start_time, ts, lat, lon, colormap):
    now = dateutil.parser.parse(ts)
    tn = find_cast(nowcasts_by_start_time, ts)
    tf = find_cast(forecasts_by_start_time, ts)
    if tn is None or tf is None:
        return None
    nowcast = nowcasts_by_start_time[tn]
    forecast = forecasts_by_start_time[tf]
    df = yranalyzer.create_combined_forecast(nowcast, forecast, 30, 24, now)
    df = yranalyzer.add_symbol_and_color(df, colormap)
    df = yranalyzer.add_day_night(df, lat, lon)
    return df


def create_wl_image(df, fn=None):
    im_wl = Image.new('RGBA', (100, 480))
    draw = ImageDraw.Draw(im_wl)
    y = 0
    for x in df.index:
        draw.rectangle([(0, y), (100, y + 20)], tuple(df["color"][x]), width=1)
        y += 20
    # Blur image a bit by resizing it twice
    im_wl = im_wl.resize((10, 48))
    im_wl = im_wl.resize((100, 508))
    if fn is not None:
        with open(fn, 'wb') as f:
            im_wl.save(f)
    return im_wl


def create_image(fn: Path, ts: datetime.datetime, im_wl: Image, args: argparse.Namespace):
    im_width, im_height = 960, 540
    im = Image.new('RGBA', (im_width, im_height), (100, 100, 100, 255))
    im_tb = Image.open(fn).convert("RGBA")
    # Draw objects on original image
    im_tb_draw = ImageDraw.Draw(im_tb)
    # Cross hair
    x, y = 339, 239  # Vanhakaupunki
    s = 50
    x1, x2, y1, y2 = x - s // 2, x + s // 2, y - s // 2, y + s // 2
    im_tb_draw.line([(x1, y), (x2, y)], width=1, fill=(255, 0, 0, 250))
    im_tb_draw.line([(x, y1), (x, y2)], width=1, fill=(255, 0, 0, 250))
    im_tb_draw.ellipse([(x1, y1), (x2, y2)], outline="red", width=2)

    # Paste original image into larger image
    im.paste(im_tb, (16, 16))
    # Create Image for drawing texts
    im_text = Image.new("RGBA", im.size, (255, 255, 255, 0))

    # Get fonts
    fnt_url = ImageFont.truetype("/System/Library/Fonts/Supplemental/Courier New Bold.ttf", 30)
    fnt_time = ImageFont.truetype("/System/Library/Fonts/Supplemental/Courier New Bold.ttf", 20)
    # fnt = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 40)
    # Get a drawing context
    d = ImageDraw.Draw(im_text)

    # draw text, half opacity
    d.text((740, 10), ts.strftime("%H:%M:%S %Z"), font=fnt_time, fill=(255, 255, 255, 150))
    d.text((740, 40), ts.strftime("%Y-%m-%d"), font=fnt_time, fill=(255, 255, 255, 150))
    # draw text, full opacity
    # d.text((10, 60), "World", font=fnt, fill=(255, 255, 255, 255))

    d.text((26, 486), "testbed.fmi.fi", font=fnt_url, fill=(0, 0, 0, 100))

    im = Image.alpha_composite(im, im_text)

    # Paste original image into larger image
    im.paste(im_wl, (630, 16))

    out_fn = Path(args.targetdir) / "testi_{}.png".format(ts.strftime("%Y%m%dT%H%M"))
    with open(out_fn, 'wb') as f:
        im.save(f)


def create_tb(tbimages, nowcasts_by_start_time, forecasts_by_start_time, args):
    for tb_image in tbimages:
        ts_utc = pytz.utc.localize(datetime.datetime.strptime(tb_image.stem, "%Y%m%d%H%M"))
        ts = ts_utc.astimezone(pytz.timezone("Europe/Helsinki"))
        ts_str = ts_utc.isoformat()
        df = create_df(nowcasts_by_start_time, forecasts_by_start_time, ts_str, lat, lon, colormap)
        if df is None:
            logging.warning(f"Couldn't create dataframe at {ts}")
            continue
        if pd.isnull(df["prec_now"][0]):
            logging.warning(f"CHECK ME: null cell found at {ts_str}")
        pd.set_option("display.max_rows", None, "display.max_columns", None, 'display.width', 1000)
        logging.info("\n" + str(df))
        im_wl = create_wl_image(df)
        create_image(tb_image, ts, im_wl, args)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments

    :return: argparse.Namespace
    """
    parser = argparse.ArgumentParser()
    parser.add_argument('--log', dest='log', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
                        default='ERROR', help='Set the logging level')
    parser.add_argument('--lat', required=True, help='Latitude in format d.ddd')
    parser.add_argument('--lon', required=True, help='Longitude in format d.ddd')
    parser.add_argument('--targetdir', required=True, help='Directory to save new images')
    parser.add_argument('--yrdirs', required=True, nargs='+', help='Directories containing JSON files from YR API')
    parser.add_argument('--tbdirs', required=True, nargs='+',
                        help='Directories containing PNG files from testbed.fmi.fi')
    # parser.add_argument('--targetdir', required=True, help='Directory to save new images')
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log), datefmt='%Y-%m-%dT%H:%M:%S',
                        format="%(asctime)s.%(msecs)03dZ %(levelname)s %(message)s")
    logging.Formatter.converter = time.gmtime  # Timestamps in UTC time
    return args


def prepare_files(args: argparse.Namespace) -> Tuple[list, list, list]:
    tbimages, nowcasts, locationforecasts = [], [], []
    logging.info("Looping testbed image directories")
    for d in args.tbdirs:
        logging.debug(f"TB: {d}")
        tbimages += get_tb_files(Path(d))
    logging.info("Looping YR data directories")
    for d in args.yrdirs:
        logging.debug(f"YR: {d}")
        c1, c2 = get_cast_files(Path(d), args.lat, args.lon)
        nowcasts += c1
        locationforecasts += c2
    return tbimages, nowcasts, locationforecasts


def main():
    args = parse_args()
    tbimages, nowcasts, locationforecasts = prepare_files(args)
    logging.info("Got {} testbed images, {} nowcasts and {} locationforecasts".format(
        len(tbimages), len(nowcasts), len(locationforecasts))
    )
    nowcasts_by_start_time = loop_casts(nowcasts)
    forecasts_by_start_time = loop_casts(locationforecasts)
    logging.info("{} nowcasts and {} locationforecasts left".format(
        len(nowcasts_by_start_time.keys()), len(forecasts_by_start_time.keys()))
    )
    create_tb(tbimages, nowcasts_by_start_time, forecasts_by_start_time, args)


if __name__ == "__main__":
    main()
