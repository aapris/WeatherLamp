"""
Create colored radar images from raw 16-bit PNG files.

Reads raw PNG files from input directory and creates colored visualizations.
Optionally adds a basemap under the radar layer.
"""

import argparse
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import contextily as ctx
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import numpy as np
from affine import Affine
from PIL import Image
from pyproj import Transformer
from rasterio.warp import Resampling, calculate_default_transform, reproject

# FMI radar composite constants (suomi_rr_eureffin / suomi_dbz_eureffin)
# CRS: EPSG:3067 (ETRS-TM35FIN)
# These values are fixed for the Finnish radar composite
FMI_RADAR_CRS = "EPSG:3067"
FMI_RADAR_BOUNDS = {
    "left": -118331.366,
    "bottom": 6335621.167,
    "right": 875567.732,
    "top": 7907751.537,
}
FMI_RADAR_SIZE = (760, 1200)  # width, height in pixels

# Available basemap providers
BASEMAP_PROVIDERS = {
    "osm": ctx.providers.OpenStreetMap.Mapnik,
    "cartodb-light": ctx.providers.CartoDB.Positron,
    "cartodb-dark": ctx.providers.CartoDB.DarkMatter,
    "esri-satellite": ctx.providers.Esri.WorldImagery,
    "esri-topo": ctx.providers.Esri.WorldTopoMap,
}


def get_args():
    parser = argparse.ArgumentParser(
        description="Create colored radar images from raw PNG files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic colored images
  python colorize_radar.py --input-dir output/2025-12-07

  # With OpenStreetMap basemap
  python colorize_radar.py --input-dir output/2025-12-07 --basemap osm

  # Crop to Helsinki area (WGS84 coordinates)
  python colorize_radar.py --input-dir output/2025-12-07 --basemap osm --bbox 24.5,59.9,25.5,60.5

  # Dark map, higher transparency
  python colorize_radar.py --input-dir output/2025-12-07 --basemap cartodb-dark --transparency 0.9
""",
    )
    parser.add_argument(
        "--input-dir",
        type=str,
        nargs="+",
        required=True,
        help="Input directory containing raw-png folder (e.g., output/2025-12-07)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        help="Output directory for colored images (default: input-dir/colored)",
    )
    parser.add_argument("--dpi", type=int, default=100, help="DPI for output images")
    parser.add_argument("--log", type=str, default="INFO", help="Logging level (ERROR, DEBUG, INFO)")

    # Basemap options
    parser.add_argument(
        "--basemap",
        type=str,
        choices=list(BASEMAP_PROVIDERS.keys()),
        help=f"Add basemap under radar. Options: {', '.join(BASEMAP_PROVIDERS.keys())}",
    )
    parser.add_argument(
        "--transparency",
        type=float,
        default=0.7,
        help="Radar layer transparency for rain pixels (0.0-1.0, default: 0.7)",
    )
    parser.add_argument(
        "--bbox",
        type=str,
        help="Bounding box in WGS84 (format: west,south,east,north e.g., 24.5,59.9,25.5,60.5)",
    )
    parser.add_argument(
        "--geotiff-info",
        type=str,
        help="Path to a GeoTIFF file to extract and display coordinate info",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log),
        datefmt="%Y-%m-%dT%H:%M:%S",
        format="%(asctime)s.%(msecs)03dZ %(levelname)s %(message)s",
    )
    logging.Formatter.converter = time.gmtime
    return args


def parse_bbox_wgs84(bbox_str: str) -> dict:
    """Parse WGS84 bbox string and convert to Web Mercator extent."""
    parts = [float(x.strip()) for x in bbox_str.split(",")]
    if len(parts) != 4:
        raise ValueError("bbox must have 4 values: west,south,east,north")

    west, south, east, north = parts

    # Convert WGS84 to Web Mercator
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    west_m, south_m = transformer.transform(west, south)
    east_m, north_m = transformer.transform(east, north)

    return {
        "wgs84": {"west": west, "south": south, "east": east, "north": north},
        "webmercator": {"west": west_m, "south": south_m, "east": east_m, "north": north_m},
    }


def parse_timestamp_from_filename(filename: str) -> datetime | None:
    """Extract timestamp from filename like radar_raw_20251207_100000.png"""
    try:
        parts = filename.replace(".png", "").split("_")
        if len(parts) >= 4:
            date_str = parts[2]
            time_str = parts[3]
            dt = datetime.strptime(f"{date_str}_{time_str}", "%Y%m%d_%H%M%S")
            return dt.replace(tzinfo=timezone.utc)
    except (ValueError, IndexError):
        pass
    return None


def load_raw_png(filepath: Path) -> np.ndarray:
    """Load raw 16-bit PNG and convert to mm/h."""
    img = Image.open(filepath)
    pixel_data = np.array(img)

    rain_mmh = pixel_data.astype(np.float32) * 0.01
    rain_mmh[pixel_data == 0] = np.nan  # No data -> transparent
    rain_mmh[pixel_data == 65535] = np.nan  # Invalid -> transparent

    return rain_mmh


def create_rain_colormap(transparency: float = 0.7):
    """Create color map: transparent → yellow → orange → red → purple."""
    colors = [
        (1.0, 1.0, 1.0, 0.0),  # transparent (no rain / nan)
        (1.0, 1.0, 0.0, transparency),  # yellow (light rain)
        (1.0, 0.65, 0.0, min(transparency + 0.1, 1.0)),  # orange (moderate rain)
        (1.0, 0.0, 0.0, min(transparency + 0.2, 1.0)),  # red (heavy rain)
        (0.5, 0.0, 0.5, 1.0),  # purple (extreme rain)
    ]

    boundaries = [0, 0.1, 0.5, 1.5, 3.0, 50]
    cmap = mcolors.LinearSegmentedColormap.from_list("rain", colors)
    cmap.set_bad(alpha=0)  # NaN values are fully transparent
    norm = mcolors.BoundaryNorm(boundaries, cmap.N, clip=True)

    return cmap, norm


def get_radar_transform(img_shape: tuple) -> Affine:
    """Get affine transform for FMI radar data based on actual image size."""
    height, width = img_shape
    bounds = FMI_RADAR_BOUNDS

    pixel_width = (bounds["right"] - bounds["left"]) / width
    pixel_height = (bounds["top"] - bounds["bottom"]) / height

    return Affine(pixel_width, 0, bounds["left"], 0, -pixel_height, bounds["top"])


def reproject_radar_to_webmercator(
    rain_mmh: np.ndarray, bbox_webmercator: dict | None = None
) -> tuple[np.ndarray, tuple]:
    """Reproject radar data from EPSG:3067 to Web Mercator (EPSG:3857)."""
    src_crs = FMI_RADAR_CRS
    dst_crs = "EPSG:3857"

    src_height, src_width = rain_mmh.shape
    src_transform = get_radar_transform(rain_mmh.shape)
    bounds = FMI_RADAR_BOUNDS

    # Calculate destination transform
    dst_transform, dst_width, dst_height = calculate_default_transform(
        src_crs,
        dst_crs,
        src_width,
        src_height,
        bounds["left"],
        bounds["bottom"],
        bounds["right"],
        bounds["top"],
    )

    # Create output array filled with NaN
    dst_data = np.full((dst_height, dst_width), np.nan, dtype=np.float32)

    reproject(
        source=rain_mmh,
        destination=dst_data,
        src_transform=src_transform,
        src_crs=src_crs,
        dst_transform=dst_transform,
        dst_crs=dst_crs,
        resampling=Resampling.nearest,
    )

    # Calculate full extent for matplotlib (left, right, bottom, top)
    left = dst_transform.c
    top = dst_transform.f
    right = left + dst_width * dst_transform.a
    bottom = top + dst_height * dst_transform.e

    full_extent = (left, right, bottom, top)

    # If bbox specified, crop to that area
    if bbox_webmercator:
        bbox = bbox_webmercator
        # Calculate pixel indices for bbox
        px_left = int((bbox["west"] - left) / dst_transform.a)
        px_right = int((bbox["east"] - left) / dst_transform.a)
        px_top = int((top - bbox["north"]) / (-dst_transform.e))
        px_bottom = int((top - bbox["south"]) / (-dst_transform.e))

        # Clamp to valid range
        px_left = max(0, px_left)
        px_right = min(dst_width, px_right)
        px_top = max(0, px_top)
        px_bottom = min(dst_height, px_bottom)

        # Crop data
        dst_data = dst_data[px_top:px_bottom, px_left:px_right]

        # Update extent to match bbox
        crop_extent = (bbox["west"], bbox["east"], bbox["south"], bbox["north"])
        return dst_data, crop_extent

    return dst_data, full_extent


def fetch_basemap_once(extent: tuple, provider, zoom: int | str = "auto") -> tuple:
    """Fetch basemap tiles once and return as image array with extent."""
    logging.info("Fetching basemap tiles (cached for subsequent frames)...")

    west, east, south, north = extent

    # Use contextily to get the basemap image
    basemap_img, basemap_extent = ctx.bounds2img(
        west,
        south,
        east,
        north,
        source=provider,
        zoom=zoom,
    )

    # ctx.bounds2img returns extent as (west, east, south, north)
    # Convert to matplotlib imshow format
    img_extent = (basemap_extent[0], basemap_extent[1], basemap_extent[2], basemap_extent[3])

    logging.info(f"  Basemap size: {basemap_img.shape[1]}x{basemap_img.shape[0]} pixels")

    return basemap_img, img_extent


def save_radar_image(
    rain_mmh: np.ndarray,
    timestamp: datetime,
    output_dir: Path,
    dpi: int = 100,
    basemap_data: tuple | None = None,
    extent: tuple | None = None,
    transparency: float = 0.7,
):
    """Save colored radar image, optionally with basemap."""
    output_dir.mkdir(parents=True, exist_ok=True)

    cmap, norm = create_rain_colormap(transparency)

    # Calculate figure aspect ratio from data or extent
    if extent is not None:
        width = extent[1] - extent[0]
        height = extent[3] - extent[2]
        aspect = width / height
        fig_height = 10
        fig_width = fig_height * aspect
    else:
        fig_width, fig_height = 8, 12

    fig, ax = plt.subplots(figsize=(fig_width, fig_height), dpi=dpi)

    if basemap_data is not None and extent is not None:
        # Draw basemap first
        basemap_img, basemap_extent = basemap_data
        ax.imshow(
            basemap_img,
            extent=basemap_extent,
            interpolation="bilinear",
            zorder=1,
        )

        # Draw radar on top - use alpha for NaN handling
        im = ax.imshow(
            rain_mmh,
            cmap=cmap,
            norm=norm,
            extent=extent,
            interpolation="nearest",
            zorder=2,
        )

        # Set axis limits to radar extent
        ax.set_xlim(extent[0], extent[1])
        ax.set_ylim(extent[2], extent[3])
    else:
        # No basemap, just radar
        im = ax.imshow(rain_mmh, cmap=cmap, norm=norm, interpolation="nearest")

    # Colorbar
    cbar = plt.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cbar.set_label("mm/h", rotation=270, labelpad=15)

    # Timestamp in title (Finnish timezone UTC+2)
    local_time = timestamp + timedelta(hours=2)
    title = f"FMI Säätutka - {local_time.strftime('%Y-%m-%d %H:%M')}"
    ax.set_title(title, fontsize=12, pad=10)
    ax.axis("off")

    # Save
    filename = f"radar_{timestamp.strftime('%Y%m%d_%H%M%S')}.png"
    filepath = output_dir / filename

    plt.tight_layout()
    plt.savefig(filepath, bbox_inches="tight", facecolor="white")
    plt.close()

    return filepath


def print_geotiff_info(geotiff_path: str):
    """Print GeoTIFF metadata for reference."""
    import rasterio

    with rasterio.open(geotiff_path) as src:
        logging.info("=" * 50)
        logging.info("GeoTIFF Information:")
        logging.info(f"  CRS: {src.crs}")
        logging.info(f"  Bounds: {src.bounds}")
        logging.info(f"  Size: {src.width} x {src.height} pixels")
        logging.info(f"  Transform: {src.transform}")
        logging.info("=" * 50)
        logging.info("\nFor colorize_radar.py, use these constants:")
        logging.info(f'  FMI_RADAR_CRS = "{src.crs}"')
        logging.info("  FMI_RADAR_BOUNDS = {")
        logging.info(f'      "left": {src.bounds.left},')
        logging.info(f'      "bottom": {src.bounds.bottom},')
        logging.info(f'      "right": {src.bounds.right},')
        logging.info(f'      "top": {src.bounds.top},')
        logging.info("  }")
        logging.info(f"  FMI_RADAR_SIZE = ({src.width}, {src.height})")

        # Also show WGS84 bounds for reference
        transformer = Transformer.from_crs(src.crs, "EPSG:4326", always_xy=True)
        west, south = transformer.transform(src.bounds.left, src.bounds.bottom)
        east, north = transformer.transform(src.bounds.right, src.bounds.top)
        logging.info("\nBounds in WGS84 (for --bbox):")
        logging.info(f"  West:  {west:.4f}")
        logging.info(f"  South: {south:.4f}")
        logging.info(f"  East:  {east:.4f}")
        logging.info(f"  North: {north:.4f}")
        logging.info(f"  Full bbox: {west:.4f},{south:.4f},{east:.4f},{north:.4f}")


def find_raw_png_files(input_dir: Path) -> list[Path]:
    """Find all raw PNG files in the input directory."""
    raw_png_dir = input_dir / "raw-png"
    if raw_png_dir.exists():
        search_dir = raw_png_dir
    else:
        search_dir = input_dir

    files = sorted(search_dir.glob("radar_raw_*.png"))
    return files


def main():
    """Create colored images from raw PNG files."""
    args = get_args()

    # If --geotiff-info provided, just print info and exit
    if args.geotiff_info:
        print_geotiff_info(args.geotiff_info)
        return
    all_raw_files = []
    for input_dir in args.input_dir:
        input_dir = Path(input_dir)
        if not input_dir.exists():
            logging.error(f"Input directory does not exist: {input_dir}")
            return
        logging.info(f"Processing input directory: {input_dir}")
        all_raw_files.extend(find_raw_png_files(input_dir))
        if not all_raw_files:
            logging.error(f"No raw PNG files found in {input_dir}")
            return

        logging.info(f"Found {len(all_raw_files)} raw PNG files")

    # Set output directory
    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        output_dir = input_dir / "colored"

    logging.info(f"Output directory: {output_dir}")

    # Parse bbox if provided
    bbox_data = None
    if args.bbox:
        try:
            bbox_data = parse_bbox_wgs84(args.bbox)
            logging.info(f"Bounding box (WGS84): {bbox_data['wgs84']}")
        except ValueError as e:
            logging.error(f"Invalid bbox: {e}")
            return

    # Prepare basemap if requested
    basemap_data = None
    radar_extent = None

    if args.basemap:
        logging.info(f"Basemap: {args.basemap}")
        logging.info(f"Transparency: {args.transparency}")

        # Load first image to get dimensions and reproject
        first_rain = load_raw_png(all_raw_files[0])
        logging.info(f"Radar image size: {first_rain.shape[1]}x{first_rain.shape[0]} pixels")

        bbox_wm = bbox_data["webmercator"] if bbox_data else None
        _, radar_extent = reproject_radar_to_webmercator(first_rain, bbox_wm)

        logging.info(f"Radar extent (Web Mercator): {radar_extent}")

        # Fetch basemap once
        provider = BASEMAP_PROVIDERS[args.basemap]
        basemap_data = fetch_basemap_once(radar_extent, provider)

        logging.info("Basemap ready, processing frames...")

    # Process each file
    output_files = []
    for i, raw_path in enumerate(all_raw_files, 1):
        timestamp = parse_timestamp_from_filename(raw_path.name)
        if timestamp is None:
            logging.warning(f"Could not parse timestamp from: {raw_path.name}")
            continue

        logging.info(f"[{i}/{len(all_raw_files)}] Processing: {timestamp}")

        try:
            rain_mmh = load_raw_png(raw_path)

            if args.basemap:
                # Reproject for basemap overlay
                bbox_wm = bbox_data["webmercator"] if bbox_data else None
                rain_reprojected, _ = reproject_radar_to_webmercator(rain_mmh, bbox_wm)
                filepath = save_radar_image(
                    rain_reprojected,
                    timestamp,
                    output_dir,
                    dpi=args.dpi,
                    basemap_data=basemap_data,
                    extent=radar_extent,
                    transparency=args.transparency,
                )
            else:
                filepath = save_radar_image(
                    rain_mmh,
                    timestamp,
                    output_dir,
                    dpi=args.dpi,
                    transparency=args.transparency,
                )

            output_files.append(filepath)
            logging.info(f"  Saved: {filepath}")
        except Exception as e:
            logging.error(f"  Error: {e}")
            import traceback

            traceback.print_exc()
            continue

    logging.info(f"\nDone! Created {len(output_files)} colored images.")
    logging.info(f"Images in directory: {output_dir}")

    # Animation instructions
    if len(output_files) > 1:
        logging.info("\nCreate animation:")
        print(
            f"  ffmpeg -framerate 4 -pattern_type glob -i '{output_dir}/radar_*.png' "
            f"-c:v libx264 -pix_fmt yuv420p radar_animation.mp4"
        )


if __name__ == "__main__":
    main()
