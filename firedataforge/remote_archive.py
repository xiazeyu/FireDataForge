"""Lazily pull single fires out of the FEDS-MTBS Zenodo zip via HTTP range requests.

The published archive (``FEDS25MTBS.zip``, ~370 MB) is served by Zenodo with HTTP
range support, and a zip stores its table of contents (the *central directory*) at
the end of the file. So we can read just the central directory, then range-fetch the
few KB belonging to one fire's GeoPackage -- no full download. When a requested fire
is absent locally, :func:`maybe_fetch_gpkg` / :func:`maybe_fetch_firepix_year` drop
it into the software cache (``cache/FEDS25MTBS/``) on demand; everything downstream
then reads local files exactly as if the full archive had been unzipped.

:func:`download_full_feds_archive` is the opposite, user-directed path: it pulls the
entire ~370 MB archive and unpacks it into the user-managed ``datasets/`` bucket.

All network failures are swallowed (returning ``None``) so the pipeline stays
fail-soft: if the fetch can't happen, the FEDS-derived layers simply skip. Set
``FIREDATAFORGE_LAZY_FETCH=0`` to disable on-demand fetching entirely.
"""

import io
import logging
import os
import threading
import urllib.request
import zipfile
from typing import Optional

from firedataforge.constants import (
    FEDS_CACHE_DIR, FEDS_DIR, FEDS_MTBS_FIRELIST_NAME, FEDS_MTBS_FIRELIST_URL,
    FEDS_MTBS_ZIP_URL, LAZY_FETCH_DEFAULT,
)
from firedataforge.progress import download_to_file, extract_zip_with_progress

log = logging.getLogger(__name__)

_USER_AGENT = "FireDataForge/0.1 (+https://github.com/xiazeyu/FireDataForge)"

# Per-process state, guarded by ``_lock``: one open remote zip (whose central
# directory we read once), a basename->member index, and a set of IDs known to be
# absent so repeated lookups never re-scan or re-hit the network.
_lock = threading.Lock()
_remote_zip: Optional[zipfile.ZipFile] = None
_member_by_basename: dict[str, str] = {}
_absent: set[str] = set()
_lazy_enabled = LAZY_FETCH_DEFAULT


def set_lazy_enabled(enabled: bool) -> None:
    """Toggle on-demand fetching (used to silence the bulk ``build_firelist`` scan)."""
    global _lazy_enabled
    _lazy_enabled = enabled


def lazy_enabled() -> bool:
    return _lazy_enabled


class _HTTPRangeFile(io.RawIOBase):
    """A seekable, read-only file object backed by HTTP range requests."""

    def __init__(self, url: str):
        self.url = url
        self.pos = 0
        # Discover the total size from a one-byte range request (HEAD is not always
        # permitted, but the Content-Range of a range GET always carries the size).
        req = urllib.request.Request(
            url, headers={"Range": "bytes=0-0", "User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            cr = resp.headers.get("Content-Range", "")
        self.size = int(cr.rsplit("/", 1)[-1]) if "/" in cr else 0
        if not self.size:
            raise OSError(f"{url} does not report a size via Content-Range")

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self.pos = offset
        elif whence == io.SEEK_CUR:
            self.pos += offset
        else:
            self.pos = self.size + offset
        return self.pos

    def tell(self) -> int:
        return self.pos

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self.size - self.pos
        if n <= 0 or self.pos >= self.size:
            return b""
        end = min(self.pos + n, self.size) - 1
        req = urllib.request.Request(
            self.url, headers={"Range": f"bytes={self.pos}-{end}", "User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
        self.pos += len(data)
        return data


def _get_remote_zip() -> Optional[zipfile.ZipFile]:
    """Open (once) the remote archive and index its members by basename.

    Returns ``None`` if the archive can't be reached, so callers stay fail-soft.
    Caller must hold ``_lock``.
    """
    global _remote_zip
    if _remote_zip is not None:
        return _remote_zip
    try:
        rf = _HTTPRangeFile(FEDS_MTBS_ZIP_URL)
        zf = zipfile.ZipFile(rf)
    except Exception as exc:  # pragma: no cover - network dependent
        log.warning(f"Could not open the FEDS-MTBS archive at {FEDS_MTBS_ZIP_URL}: {exc}")
        return None
    _remote_zip = zf
    for name in zf.namelist():
        if not name.endswith("/"):
            _member_by_basename[os.path.basename(name)] = name
    log.info(f"Opened remote FEDS-MTBS archive ({len(_member_by_basename)} files indexed)")
    return zf


def _extract_member(zf: zipfile.ZipFile, member: str, dest_path: str) -> str:
    """Range-extract one zip member to ``dest_path`` atomically. Caller holds ``_lock``."""
    data = zf.read(member)
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    tmp = f"{dest_path}.part"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, dest_path)
    log.info(f"Fetched {member} ({len(data)/1024:.0f} KB) -> {dest_path}")
    return dest_path


def maybe_fetch_gpkg(
    event_id: str, year: Optional[int] = None, feds_dir: str = FEDS_CACHE_DIR
) -> Optional[str]:
    """Range-fetch ``<event_id>.gpkg`` from the Zenodo archive, or ``None``.

    Drops the fire into the software cache (``feds_dir``, ``cache/FEDS25MTBS/``
    by default), preserving the archive's ``<year>/<event_id>.gpkg`` layout.
    Returns the local path on success. No-op (``None``) when lazy fetching is
    disabled, the fire is not in the archive, or the network is unavailable.
    """
    if not _lazy_enabled:
        return None
    key = f"gpkg:{event_id}"
    # When the year is known the cache path is predictable; short-circuit on a
    # local hit before any network round-trip (mirrors maybe_fetch_firepix_year).
    if year is not None:
        dest = os.path.join(feds_dir, str(year), f"{event_id}.gpkg")
        if os.path.exists(dest):
            return dest
    with _lock:
        if key in _absent:
            return None
        zf = _get_remote_zip()
        if zf is None:
            return None
        # Resolve by basename so any archive directory layout (with or without a
        # top-level wrapper folder) works; prefer the conventional <year>/ path.
        member = None
        if year is not None and f"{year}/{event_id}.gpkg" in zf.NameToInfo:
            member = f"{year}/{event_id}.gpkg"
        else:
            member = _member_by_basename.get(f"{event_id}.gpkg")
        if member is None:
            _absent.add(key)
            return None
        # Preserve the archive's <year>/<event_id>.gpkg layout under the local root.
        dest = os.path.join(feds_dir, member)
        if os.path.exists(dest):
            return dest
        try:
            return _extract_member(zf, member, dest)
        except Exception as exc:  # pragma: no cover - network dependent
            log.warning(f"Could not fetch {member}: {exc}")
            return None


def maybe_fetch_firepix_year(year: int, firepix_dir: str) -> Optional[str]:
    """Range-fetch ``firepix/Firepix_<year>.csv`` from the Zenodo archive, or ``None``."""
    if not _lazy_enabled:
        return None
    key = f"firepix:{year}"
    basename = f"Firepix_{year}.csv"
    dest = os.path.join(firepix_dir, basename)
    with _lock:
        if os.path.exists(dest):
            return dest
        if key in _absent:
            return None
        zf = _get_remote_zip()
        if zf is None:
            return None
        # Match by basename so the entry resolves regardless of the archive's
        # internal directory layout (e.g. with or without a wrapper folder).
        member = _member_by_basename.get(basename)
        if member is None:
            _absent.add(key)
            return None
        try:
            return _extract_member(zf, member, dest)
        except Exception as exc:  # pragma: no cover - network dependent
            log.warning(f"Could not fetch {member}: {exc}")
            return None


def _stream_download(
    url: str, dest_path: str, timeout: int = 600, connections: int = 1,
) -> str:
    """Stream ``url`` to ``dest_path`` atomically, with a progress bar."""
    return download_to_file(
        url, dest_path, desc=os.path.basename(dest_path), timeout=timeout,
        connections=connections)


def download_feds_firelist(dest_dir: str = FEDS_DIR) -> Optional[str]:
    """Download the FEDS-MTBS summary fire list into ``dest_dir``.

    Fetches the ~280 MB ``fireslist_FEDS25MTBS_2012-2024.geojson`` -- a separate
    file in the same Zenodo record as the zip (it is **not** inside the zip) --
    used for fully offline event resolution. Skips the download if it already
    exists. Returns the local path, or ``None`` on failure (fail-soft).
    """
    dest = os.path.join(dest_dir, FEDS_MTBS_FIRELIST_NAME)
    if os.path.exists(dest):
        log.info(f"FEDS-MTBS fire list already present at {dest}")
        return dest
    log.info(f"Downloading the FEDS-MTBS fire list from {FEDS_MTBS_FIRELIST_URL}")
    try:
        # Zenodo throttles per connection; two parallel range streams roughly
        # saturate the link instead of crawling at the single-stream ~1 MB/s.
        _stream_download(FEDS_MTBS_FIRELIST_URL, dest, connections=2)
    except Exception as exc:  # pragma: no cover - network dependent
        log.warning(
            f"Could not download the FEDS-MTBS fire list ({exc}); offline event "
            "resolution will fall back to the MTBS fire list / mtbs.gov.")
        return None
    log.info(f"FEDS-MTBS fire list saved to {dest}")
    return dest


def download_full_feds_archive(
    dest_dir: str = FEDS_DIR, include_firelist: bool = False
) -> str:
    """Download and unpack the *entire* FEDS-MTBS archive into ``dest_dir``.

    The opposite of the lazy per-fire path: pulls the full ~370 MB
    ``FEDS25MTBS.zip`` from Zenodo and extracts every GeoPackage and the firepix
    CSVs into the user-managed ``datasets/`` bucket so all fires resolve locally
    with no further network access. Use this when trading disk for bandwidth (e.g.
    processing many fires); otherwise the on-demand range fetch into ``cache/`` is
    enough.

    The GeoPackages already carry each fire's geometry, active-burning window, and
    bounds, so the only thing they lack is the human-readable fire name + acreage.
    That metadata is a *separate* choice (see :func:`download_feds_firelist` and the
    setup wizard), so ``include_firelist`` defaults to ``False`` -- the 280 MB
    ``fireslist_FEDS25MTBS_2012-2024.geojson`` is fetched only when explicitly
    requested.

    Returns ``dest_dir``.
    """
    os.makedirs(dest_dir, exist_ok=True)
    tmp_zip = os.path.join(dest_dir, "_FEDS25MTBS.zip")
    log.info(f"Downloading the full FEDS-MTBS archive from {FEDS_MTBS_ZIP_URL}")
    try:
        # Two parallel range streams bypass Zenodo's per-connection throttle.
        download_to_file(
            FEDS_MTBS_ZIP_URL, tmp_zip, desc="FEDS-MTBS archive", connections=2)
        with zipfile.ZipFile(tmp_zip) as zf:
            extract_zip_with_progress(zf, dest_dir, desc="Extracting FEDS-MTBS")
    finally:
        if os.path.exists(tmp_zip):
            os.remove(tmp_zip)
    log.info(f"Full FEDS-MTBS archive unpacked into {dest_dir}")
    # The summary fire list is a separate Zenodo file (not inside the zip).
    if include_firelist:
        download_feds_firelist(dest_dir)
    return dest_dir
