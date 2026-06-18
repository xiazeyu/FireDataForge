"""Download the FireDataForge example bundle from Zenodo and unpack it at the root.

The example bundle (``examples.zip``) is the eight demo fires -- plus their
firepix and an offline fire list -- that the repo's examples, benchmark, and
validation reference. It is a self-contained working directory: it carries a
``datasets/FEDS25MTBS/`` prefix and a top-level ``events.txt``, so it unzips at
the repository root, dropping the fires straight into ``datasets/FEDS25MTBS/``.
It is hosted on our own Zenodo record so users can run everything without the
full ~370 MB FEDS-MTBS archive; the full dataset stays at the FEDS-MTBS record.
"""

import json
import logging
import os
import urllib.request
import zipfile

from firedataforge.constants import (
    CACHE_DIR, EXAMPLES_UNZIP_ROOT, EXAMPLES_ZENODO_RECORD, EXAMPLES_ZIP_NAME,
)
from firedataforge.progress import download_to_file, extract_zip_with_progress

log = logging.getLogger(__name__)


def _zenodo_file_url(record_id: str, filename: str) -> str:
    """Resolve the download URL for ``filename`` in a Zenodo record.

    Queries the record's API for the file's canonical link, falling back to the
    conventional ``/records/<id>/files/<name>`` path if the API is unreachable.
    """
    api = f"https://zenodo.org/api/records/{record_id}"
    try:
        with urllib.request.urlopen(api, timeout=30) as resp:
            meta = json.load(resp)
        for entry in meta.get("files", []):
            if entry.get("key") == filename:
                link = entry.get("links", {}).get("self")
                if link:
                    return link
    except Exception as exc:  # pragma: no cover - network dependent
        log.debug(f"Zenodo API lookup failed ({exc}); using the direct URL pattern")
    return f"https://zenodo.org/records/{record_id}/files/{filename}?download=1"


def examples_record_configured(record_id: str = EXAMPLES_ZENODO_RECORD) -> bool:
    """True once a real example-bundle Zenodo record id has been set."""
    return bool(record_id) and record_id != "0000000"


def fetch_examples(
    dest_dir: str = EXAMPLES_UNZIP_ROOT,
    record_id: str = EXAMPLES_ZENODO_RECORD,
    filename: str = EXAMPLES_ZIP_NAME,
    overwrite: bool = False,
) -> str:
    """Download and unpack the example bundle at ``dest_dir`` (the repo root).

    Fetches ``filename`` (``examples.zip``) from the FireDataForge example Zenodo
    record and extracts it at ``dest_dir`` -- the repository root by default. The
    archive carries its own ``datasets/FEDS25MTBS/`` prefix and a top-level
    ``events.txt``, so unpacking it here lays down the eight demo fires, their
    firepix CSVs, and the offline ``fireslist_examples.csv`` under
    ``datasets/FEDS25MTBS/`` and writes ``events.txt`` at the root.

    Args:
        dest_dir: Directory to unpack into (the repo root; the bundle supplies
            the ``datasets/FEDS25MTBS/`` sub-path itself).
        record_id: Zenodo record id holding the bundle.
        filename: Bundle file name within the record.
        overwrite: Replace files that already exist locally (default: keep them).

    Returns:
        ``dest_dir``.

    Raises:
        RuntimeError: If the record id has not been configured yet.
    """
    if not examples_record_configured(record_id):
        raise RuntimeError(
            "The example-bundle Zenodo record id is not set. Upload the example "
            "bundle, then set EXAMPLES_ZENODO_RECORD in firedataforge/constants.py "
            "(or the FIREDATAFORGE_EXAMPLES_RECORD environment variable)."
        )
    url = _zenodo_file_url(record_id, filename)
    log.info(f"Downloading example bundle from {url}")
    os.makedirs(dest_dir, exist_ok=True)
    # Stage the zip in the software-managed cache (gitignored, safe to delete) so
    # an interrupted download never leaves a stray archive at the repo root.
    tmp_zip = os.path.join(CACHE_DIR, f"_{filename}")
    try:
        download_to_file(url, tmp_zip, desc="Example bundle", timeout=300)
        with zipfile.ZipFile(tmp_zip) as zf:
            members = zf.namelist()
            if not overwrite:
                members = [
                    m for m in members
                    if m.endswith("/") or not os.path.exists(os.path.join(dest_dir, m))
                ]
            extract_zip_with_progress(
                zf, dest_dir, members=members, desc="Extracting examples")
    finally:
        if os.path.exists(tmp_zip):
            os.remove(tmp_zip)
    log.info(f"Example bundle unpacked into {dest_dir}")
    return dest_dir
