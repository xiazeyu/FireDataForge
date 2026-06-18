"""Filesystem paths and source-API constants for FireDataForge."""

import os


# Two on-disk buckets, both optional and gitignored:
#   DATASETS_DIR -- user-managed. Full archives the user deliberately downloads
#                   and unpacks (via ``--setup`` or by hand). FireDataForge only
#                   writes here on an explicit, user-requested download.
#   CACHE_DIR    -- software-managed. Everything fetched on the fly (per-fire
#                   GeoPackages, FIRMS slices, HRRR GRIBs, WUI tiles, the MTBS
#                   fire list) lands here. Safe to delete at any time; it is
#                   transparently re-fetched on the next run.
# Readers check DATASETS_DIR first, then CACHE_DIR; on-the-fly fetches only ever
# write to CACHE_DIR, so the two buckets never conflict.
DATASETS_DIR = os.environ.get("FIREDATAFORGE_DATASETS", "datasets")
CACHE_DIR = os.environ.get("FIREDATAFORGE_CACHE", "cache")

# Fixed subfolder / file names for each on-the-fly download bucket, relative to
# the cache root. The cache root is user-settable per run (ProcessingArgs.
# cache_dir / --cache_dir, default CACHE_DIR); these names are fixed, so every
# cached dataset keeps a stable, predictable location under whatever root the
# user picks. The DEFAULT_* paths below are just these names joined to the
# default CACHE_DIR; pass a different root to relocate the whole cache at once.
HERBIE_CACHE_NAME = "herbie"
FIRMS_CACHE_NAME = "FIRMS"
FEDS_CACHE_NAME = "FEDS25MTBS"
FIREPIX_CACHE_NAME = os.path.join(FEDS_CACHE_NAME, "firepix")
GLOBALWUI_CACHE_NAME = "GlobalWUI"
FIRELIST_CACHE_NAME = "mtbs_firelist.csv"

# Filesystem root the example bundle (``examples.zip``) unpacks into. The bundle
# is a self-contained working directory: it carries a ``datasets/FEDS25MTBS/``
# prefix *and* a top-level ``events.txt``, so it must be extracted one level
# *above* DATASETS_DIR -- the repo root for the default layout -- not inside the
# FEDS folder. Derived from DATASETS_DIR so it tracks the same root the readers
# resolve their relative paths against (``"."`` for the default ``datasets``).
EXAMPLES_UNZIP_ROOT = os.path.dirname(DATASETS_DIR) or "."

# NASA FIRMS: optional user-placed full-archive CSVs live here; on-the-fly
# Area-API slices are cached per event under ``<cache_dir>/FIRMS/``.
DEFAULT_FIRMS_DIR = os.path.join(DATASETS_DIR, "FIRMS")


# User-managed FEDS25MTBS archive (optional pre-staged full download):
# perimeter/fireline GeoPackages, firepix CSVs, fire list. The folder name
# matches the released archive (``FEDS25MTBS.zip``) exactly -- no legacy
# folder-name variants are honoured.
FEDS_DIR = os.path.join(DATASETS_DIR, "FEDS25MTBS")
DEFAULT_FIREPIX_DIR = os.path.join(FEDS_DIR, "firepix")

# Software-managed cache for fires/firepix range-fetched on demand from the
# Zenodo archive (mirrors the ``<year>/<event>.gpkg`` + ``firepix/`` layout).
FEDS_CACHE_DIR = os.path.join(CACHE_DIR, FEDS_CACHE_NAME)

# FEDS-MTBS dataset (Chen et al.), published on Zenodo: the full 7,739-fire,
# 2012-2024 archive (GeoPackages + firepix) plus the summary fire list. The
# Zenodo record carries the *updated* FEDS-MTBS dataset (concept DOI
# 10.5281/zenodo.20187962); the algorithm/FEDS reference is Chen et al. 2022,
# Sci. Data, doi:10.1038/s41597-022-01343-0 (see the README citation table).
FEDS_MTBS_ZENODO_DOI = "10.5281/zenodo.20187962"
FEDS_MTBS_ZENODO_RECORD = "20187963"
FEDS_MTBS_ZENODO_URL = f"https://zenodo.org/records/{FEDS_MTBS_ZENODO_RECORD}"
FEDS_MTBS_CHEN_DOI = "10.1038/s41597-022-01343-0"
# The archive zip inside that record. Zenodo serves files with HTTP range support,
# so a single fire's GeoPackage (or a year's firepix) can be pulled out of this zip
# on demand without downloading all ~370 MB (see firedataforge.remote_archive).
FEDS_MTBS_ZIP_NAME = "FEDS25MTBS.zip"
FEDS_MTBS_ZIP_URL = (
    f"https://zenodo.org/records/{FEDS_MTBS_ZENODO_RECORD}/files/{FEDS_MTBS_ZIP_NAME}"
)
# The summary fire list shipped alongside the zip in the same record (a separate
# ~280 MB GeoPackage of MTBS final perimeters + metadata for all 7,739 fires).
# Optional: place it under ``datasets/FEDS25MTBS/`` for fully offline event
# resolution. It is NOT inside FEDS25MTBS.zip, so the full-archive download does
# not include it.
FEDS_MTBS_FIRELIST_NAME = "fireslist_FEDS25MTBS_2012-2024.geojson"
FEDS_MTBS_FIRELIST_URL = (
    f"https://zenodo.org/records/{FEDS_MTBS_ZENODO_RECORD}/files/{FEDS_MTBS_FIRELIST_NAME}"
)
# Lazily range-fetch missing fires from FEDS_MTBS_ZIP_URL when they are requested
# but absent locally. On by default; set FIREDATAFORGE_LAZY_FETCH=0 to force a
# fully offline / deterministic run (missing FEDS layers then just skip).
LAZY_FETCH_DEFAULT = os.environ.get("FIREDATAFORGE_LAZY_FETCH", "1") not in ("0", "false", "False", "")

# FireDataForge example / reproducibility bundle (our own Zenodo record): the
# eight demo fires used by the examples/benchmark/validation, plus the reference
# benchmark/validation outputs. Fetched by ``python main.py --fetch-examples``.
# EXAMPLES_ZENODO_DOI is the v1.0.0 version DOI (its concept/all-versions DOI is
# 10.5281/zenodo.20743742); EXAMPLES_ZENODO_RECORD is the matching version record
# id the download URL needs -- here it equals the version-DOI number. Override the
# record at runtime with FIREDATAFORGE_EXAMPLES_RECORD.
EXAMPLES_ZENODO_DOI = "10.5281/zenodo.20743743"
EXAMPLES_ZENODO_RECORD = os.environ.get(
    "FIREDATAFORGE_EXAMPLES_RECORD", "20743743"
)
EXAMPLES_ZENODO_URL = f"https://zenodo.org/records/{EXAMPLES_ZENODO_RECORD}"
# The bundle file within that record. It is a self-contained working directory:
# unzipping it at the repo root (see EXAMPLES_UNZIP_ROOT) lays down
# ``datasets/FEDS25MTBS/<year>/<event>.gpkg`` + ``firepix/`` + the offline
# ``fireslist_examples.csv``, plus a top-level ``events.txt``.
EXAMPLES_ZIP_NAME = "examples.zip"

# Self-built MTBS fire list: the offline fallback for events absent from the
# bundled FEDS fire list. Software-managed (grown one event at a time at run time
# and (re)built in bulk by ``build_firelist`` / ``--build-firelist``), so it
# lives in the cache. Kept outside the FEDS archive folder so it never shadows
# the bundled FEDS fire list (whose bbox is the FEDS perimeter extent, preferred
# over the MTBS burn-boundary bbox).
DEFAULT_FIRELIST_CACHE = os.path.join(CACHE_DIR, FIRELIST_CACHE_NAME)

# Fallback processing-window length (days) used when no FEDS perimeter and no
# explicit end date are available: the window is anchored on the MTBS ignition
# date and bounded to this many days. Long enough to cover most fires' active
# period while keeping the HRRR weather download bounded (t_end's only
# cost-sensitive consumer).
DEFAULT_FIRE_WINDOW_DAYS = 15

# FIRMS data files (VIIRS)
FIRMS_FILES = [
    "fire_archive_SV-C2_708942.csv",  # VIIRS archive 2025
    "fire_nrt_SV-C2_708942.csv",       # VIIRS near-real-time 2025
    "fire_archive_SV-C2_713904.csv" ,  # VIIRS archive 2024
]

# Columns to load from FIRMS CSV (reduces memory and speeds up loading)
FIRMS_USECOLS = ['latitude', 'longitude', 'acq_date', 'acq_time', 'frp', 'confidence', 'daynight']

# NASA FIRMS Area API: fetches only the active-fire points inside a bounding box
# and date window, so the full multi-GB archive CSVs never need to be downloaded.
# A free MAP_KEY (https://firms.modaps.eosdis.nasa.gov/api/map_key/) is required;
# supply it via the FIRMS_MAP_KEY (or MAP_KEY) environment variable.
# Docs: https://firms.modaps.eosdis.nasa.gov/api/area/
FIRMS_API_BASE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
# VIIRS Collection 2 active fire. Both S-NPP (matches the bundled "SV-C2" CSVs)
# and NOAA-20 platforms are queried, each with standard processing (archive) and
# near-real-time, then merged + de-duplicated. Querying multiple platforms is
# important: e.g. S-NPP VIIRS had a multi-day outage during the July 2024 Park
# Fire (0 detections) that NOAA-20 captured in full. Sources outside their
# availability window simply return an empty response and are skipped.
FIRMS_API_SOURCES = [
    "VIIRS_SNPP_SP", "VIIRS_SNPP_NRT",
    "VIIRS_NOAA20_SP", "VIIRS_NOAA20_NRT",
]
FIRMS_API_MAX_DAYS = 5  # Area API allows at most a 5-day window per request


__all__ = [
    'DATASETS_DIR', 'CACHE_DIR', 'EXAMPLES_UNZIP_ROOT',
    'HERBIE_CACHE_NAME', 'FIRMS_CACHE_NAME', 'FEDS_CACHE_NAME',
    'FIREPIX_CACHE_NAME', 'GLOBALWUI_CACHE_NAME', 'FIRELIST_CACHE_NAME',
    'DEFAULT_FIRMS_DIR',
    'FEDS_DIR', 'DEFAULT_FIREPIX_DIR', 'FEDS_CACHE_DIR',
    'DEFAULT_FIRELIST_CACHE', 'DEFAULT_FIRE_WINDOW_DAYS',
    'FEDS_MTBS_ZENODO_DOI', 'FEDS_MTBS_ZENODO_RECORD', 'FEDS_MTBS_ZENODO_URL',
    'FEDS_MTBS_CHEN_DOI', 'FEDS_MTBS_ZIP_NAME', 'FEDS_MTBS_ZIP_URL',
    'FEDS_MTBS_FIRELIST_NAME', 'FEDS_MTBS_FIRELIST_URL', 'LAZY_FETCH_DEFAULT',
    'EXAMPLES_ZENODO_DOI', 'EXAMPLES_ZENODO_RECORD', 'EXAMPLES_ZENODO_URL',
    'EXAMPLES_ZIP_NAME',
    'FIRMS_FILES', 'FIRMS_USECOLS', 'FIRMS_API_BASE',
    'FIRMS_API_SOURCES', 'FIRMS_API_MAX_DAYS',
]
