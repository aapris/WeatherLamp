"""
Download FMI radar data and store GeoTIFF and raw PNG files.

Output directory structure:
  output/
    2025-12-07/
      geotiff/
        radar_suomi_rr_eureffin_20251207T100000Z.geotiff[.gz]
      raw-png/
        radar_raw_20251207_100000.png
"""

import argparse
import gzip
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import rasterio
from PIL import Image


def get_args():
    parser = argparse.ArgumentParser(description="Download FMI radar data and store raw files")
    parser.add_argument("--start-time", type=str, help="Start time in ISO format")
    parser.add_argument("--end-time", type=str, help="End time in ISO format")
    parser.add_argument("--duration", type=int, default=60, help="Duration in minutes")
    parser.add_argument("--layer", type=str, default="rr", help="Radar layer (rr, dbz)")
    parser.add_argument("--output-dir", type=str, default="output", help="Output base directory")
    parser.add_argument("--gzip-geotiff", type=bool, default=True, help="Gzip GeoTIFF file (default: True)")
    parser.add_argument("--log", type=str, default="INFO", help="Logging level (ERROR, DEBUG, INFO)")
    args = parser.parse_args()

    if args.end_time:
        args.end_time = datetime.fromisoformat(args.end_time)
    else:
        args.end_time = datetime.now(timezone.utc)
    args.end_time = args.end_time.replace(second=0, microsecond=0)

    if args.start_time:
        args.start_time = datetime.fromisoformat(args.start_time)
    else:
        args.start_time = args.end_time - timedelta(minutes=args.duration)

    logging.basicConfig(
        level=getattr(logging, args.log),
        datefmt="%Y-%m-%dT%H:%M:%S",
        format="%(asctime)s.%(msecs)03dZ %(levelname)s %(message)s",
    )
    logging.Formatter.converter = time.gmtime
    return args


def get_geotiff_filename_from_url(url: str) -> str:
    """Extract filename from GeoTIFF URL.

    URL like:
    https://openwms.fmi.fi/geoserver/Radar/wms?...&layers=Radar:suomi_rr_eureffin&time=2025-12-07T10:00:00Z
    returns: radar_suomi_rr_eureffin_20251207T100000Z.geotiff
    """
    parsed_url = urlparse(url)
    query_params = parse_qs(parsed_url.query)

    layers = query_params.get("layers", [None])[0]
    if not layers:
        return None  # type: ignore

    layer = layers.replace("Radar:", "").replace(":", "_").lower()

    time_param = query_params.get("time", [None])[0]
    if not time_param:
        return None  # type: ignore

    timestamp = time_param.replace("-", "").replace(":", "")
    return f"radar_{layer}_{timestamp}.geotiff"


def fetch_radar_urls(start_time, end_time, layer="rr"):
    """Fetch radar GeoTIFF URLs from WFS.

    Args:
        start_time: datetime object (UTC)
        end_time: datetime object (UTC)
        layer: 'rr' (rainfall intensity), 'dbz' (reflectivity), etc.

    Returns:
        List of tuples: [(timestamp, geotiff_url), ...]
    """
    stored_query = f"fmi::radar::composite::{layer}"

    start_time_str = start_time.strftime("%Y-%m-%dT%H:%M:00")
    end_time_str = end_time.strftime("%Y-%m-%dT%H:%M:00")
    wfs_url = (
        f"https://opendata.fmi.fi/wfs?"
        f"service=WFS&version=2.0.0&request=getFeature"
        f"&storedquery_id={stored_query}"
        f"&starttime={start_time_str}Z"
        f"&endtime={end_time_str}Z"
    )
    logging.debug(wfs_url)
    logging.info(f"Fetching WFS data: {start_time_str} - {end_time_str}")

    response = httpx.get(wfs_url, timeout=30)
    response.raise_for_status()

    root = ET.fromstring(response.content)

    namespaces = {
        "wfs": "http://www.opengis.net/wfs/2.0",
        "gml": "http://www.opengis.net/gml/3.2",
        "om": "http://www.opengis.net/om/2.0",
    }

    urls = []
    for member in root.findall(".//wfs:member", namespaces):
        time_elem = member.find(".//gml:timePosition", namespaces)
        if time_elem is None:
            continue

        time_str = time_elem.text
        timestamp = datetime.fromisoformat(time_str.replace("Z", "+00:00"))

        file_ref = member.find(".//gml:fileReference", namespaces)
        if file_ref is not None:
            url = file_ref.text.strip()
            urls.append((timestamp, url))

    logging.info(f"Found {len(urls)} radar images")
    return sorted(urls)


def download_radar(url, geotiff_dir: Path, raw_png_dir: Path, timestamp: datetime, gzip_geotiff: bool = True):
    """Download GeoTIFF and save both original and raw PNG."""
    response = httpx.get(url, timeout=30)
    response.raise_for_status()

    # Save original GeoTIFF
    geotiff_filename = get_geotiff_filename_from_url(url)
    if geotiff_filename:
        geotiff_path = geotiff_dir / geotiff_filename
        if gzip_geotiff:
            gz_path = geotiff_path.with_suffix(geotiff_path.suffix + ".gz")
            with gzip.open(gz_path, "wb") as f:
                f.write(response.content)
        else:
            with open(geotiff_path, "wb") as f:
                f.write(response.content)
        logging.info(f"  Saved GeoTIFF: {geotiff_path}")
    else:
        logging.warning(f"Could not get filename from URL: {url}")

    # Extract pixel data and save as raw PNG
    with rasterio.open(BytesIO(response.content)) as src:
        pixel_data = src.read(1)

    raw_filename = f"radar_raw_{timestamp.strftime('%Y%m%d_%H%M%S')}.png"
    raw_path = raw_png_dir / raw_filename

    img = Image.fromarray(pixel_data)
    img.save(raw_path)
    logging.info(f"  Saved raw PNG: {raw_path}")

    return geotiff_path, raw_path


def create_output_dirs(base_dir: str, date: datetime) -> tuple[Path, Path]:
    """Create date-based output directory structure."""
    base_path = Path(base_dir)
    date_str = date.strftime("%Y-%m-%d")

    date_dir = base_path / date_str
    geotiff_dir = date_dir / "geotiff"
    raw_png_dir = date_dir / "raw-png"

    geotiff_dir.mkdir(parents=True, exist_ok=True)
    raw_png_dir.mkdir(parents=True, exist_ok=True)

    return geotiff_dir, raw_png_dir


def main():
    """Download radar data and store files."""
    args = get_args()

    radar_urls = fetch_radar_urls(args.start_time, args.end_time, layer=args.layer)

    if not radar_urls:
        logging.error("No radar data found!")
        return

    # Group by date and create directories
    dates_seen = set()
    for timestamp, _ in radar_urls:
        dates_seen.add(timestamp.date())

    # Create directories for all dates
    dir_cache = {}
    for date in dates_seen:
        geotiff_dir, raw_png_dir = create_output_dirs(args.output_dir, datetime.combine(date, datetime.min.time()))
        dir_cache[date] = (geotiff_dir, raw_png_dir)
        logging.info(f"Output directory: {geotiff_dir.parent}")

    # Download all images
    downloaded = 0
    for i, (timestamp, url) in enumerate(radar_urls, 1):
        logging.info(f"[{i}/{len(radar_urls)}] Downloading: {timestamp}")

        try:
            geotiff_dir, raw_png_dir = dir_cache[timestamp.date()]
            download_radar(url, geotiff_dir, raw_png_dir, timestamp, gzip_geotiff=args.gzip_geotiff)
            downloaded += 1
        except Exception as e:
            logging.error(f"  Error: {e}")
            continue

    logging.info(f"\nDone! Downloaded {downloaded} radar images.")
    for date in sorted(dates_seen):
        logging.info(f"  {args.output_dir}/{date.strftime('%Y-%m-%d')}/")


if __name__ == "__main__":
    main()
