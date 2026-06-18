"""Persistence of harmonized layers (.npy) and grid coordinates."""

import logging
import os
from dataclasses import asdict, replace

import numpy as np

from schemas import DataLayer, GeoReference, ProcessingTask

log = logging.getLogger(__name__)


def save_numpy(
    task_info: ProcessingTask,
    data: DataLayer,
    output_dir: str = 'output'
) -> None:
    """Save processed data to a numpy file.

    Creates a directory structure: output_dir/event_id/data_name.npy

    Args:
        task_info: Task configuration containing event_id.
        data: Data to save with metadata.
        output_dir: Base output directory.
    """
    event_id = task_info.event_id
    output_path = os.path.join(output_dir, event_id)
    os.makedirs(output_path, exist_ok=True)

    output_path = os.path.join(output_path, f"{data.name}.npy")
    # Stamp the resolution of the grid this layer currently sits on, so each file
    # is self-describing (native_resolution is the source's own resolution). Most
    # layers are resampled to the task grid; a builder that targets a different
    # grid (e.g. the coarser HRRR weather grid) sets current_resolution itself,
    # so only fill it in when the builder left it unset.
    if data.current_resolution is None:
        data = replace(data, current_resolution=task_info.resolution)
    np.save(output_path, asdict(data))

    log.info(f"Saved {data.name} data to {output_path}")


def load_numpy(filepath: str) -> DataLayer:
    """Load processed data from a numpy file.

    Args:
        filepath: Path to the .npy file.

    Returns:
        DataLayer object with loaded data.
    """
    loaded_dict = np.load(filepath, allow_pickle=True).item()
    # asdict() flattens nested dataclasses to dicts on save; rehydrate the
    # typed GeoReference here so loads round-trip to the same types as saves.
    geo = loaded_dict.get("georeference")
    if isinstance(geo, dict):
        loaded_dict["georeference"] = GeoReference(**geo)
    obj = DataLayer(**loaded_dict)
    return obj


def save_coordinates(
    task_info: ProcessingTask,
    output_dir: str = 'output'
) -> None:
    """Save pixel-center coordinate arrays and CRS for the task grid.

    Writes ``coordinates.npy`` into ``output_dir/event_id/`` containing a
    :class:`DataLayer` with:

    - ``data[0]``: 1-D array of x (easting/longitude) pixel-center coordinates,
      shape ``(width,)``.
    - ``data[1]``: 1-D array of y (northing/latitude) pixel-center coordinates,
      shape ``(height,)``, ordered top-to-bottom to match raster row order.
    - ``georeference``: a :class:`~schemas.GeoReference` with ``crs`` (short id,
      e.g. ``"EPSG:5070"``), ``crs_wkt`` / ``crs_proj4`` / ``crs_epsg``
      (self-contained CRS definitions for archival / custom-CRS use),
      ``bounds`` (minx, miny, maxx, maxy), ``shape`` (height, width),
      ``resolution``, and ``transform`` (affine coefficients
      ``a, b, c, d, e, f``).

    These coordinates correspond to the same grid every other ``.npy`` layer
    is sampled on, so researchers can wrap arrays directly into ``xarray`` or
    re-project them using the saved CRS for publication-quality figures.

    Args:
        task_info: Task configuration with ``bounds``, ``shape``, and ``crs``.
        output_dir: Base output directory.
    """
    minx, miny, maxx, maxy = task_info.bounds
    height, width = task_info.shape

    px = (maxx - minx) / width if width else 0.0
    py = (maxy - miny) / height if height else 0.0

    # Pixel-center coordinates. Y is top-to-bottom (north -> south) to match
    # rasterio/numpy row-major raster ordering used elsewhere in the pipeline.
    x = minx + (np.arange(width) + 0.5) * px
    y = maxy - (np.arange(height) + 0.5) * py

    # Affine transform (from_origin equivalent): pixel (col, row) -> (x, y).
    transform = (px, 0.0, minx, 0.0, -py, maxy)

    # Self-contained CRS info so consumers don't need an EPSG lookup or
    # network access to reconstruct the projection (useful for archival,
    # custom CRSes, or environments without a PROJ database).
    try:
        from pyproj import CRS as _CRS
        _crs_obj = _CRS.from_user_input(task_info.crs)
        crs_wkt = _crs_obj.to_wkt()
        crs_proj4 = _crs_obj.to_proj4()
        crs_epsg = _crs_obj.to_epsg()
    except Exception:  # pragma: no cover - defensive only
        crs_wkt = None
        crs_proj4 = None
        crs_epsg = None

    coords = DataLayer(
        name="coordinates",
        data=[x, y],
        timestamps=None,
        source="Derived from ProcessingTask grid",
        native_resolution=task_info.resolution,
        unit="CRS units",
        georeference=GeoReference(
            crs=task_info.crs,
            bounds=task_info.bounds,
            shape=task_info.shape,
            resolution=task_info.resolution,
            transform=transform,
            crs_wkt=crs_wkt,
            crs_proj4=crs_proj4,
            crs_epsg=crs_epsg,
        ),
        note={"axes": ["x (width)", "y (height, top-to-bottom)"]},
    )

    save_numpy(task_info, coords, output_dir)
