"""Progress reporting for long-running downloads and bulk extraction.

Wraps the ``urllib`` downloads and zip extraction used across FireDataForge with
``tqdm`` bars. Every bar is created with ``disable=None``, so it shows live on an
interactive terminal but auto-hides whenever stderr is not a TTY (logs, pipes,
HPC batch jobs) -- the existing INFO logging then carries the same information.
Bars use ``leave=False`` so a finished transfer leaves no clutter behind.

Everything stays best-effort and matches the rest of the codebase's fail-soft
style: a server that omits ``Content-Length`` simply yields an indeterminate
(byte-counting) bar rather than an error.
"""

import os
import threading
import urllib.error
import urllib.request
import zipfile
from typing import Optional

from tqdm import tqdm

_USER_AGENT = "FireDataForge/0.1 (+https://github.com/xiazeyu/FireDataForge)"

# 1 MiB read/copy block -- matches the buffer the old shutil.copyfileobj calls used.
_CHUNK = 1024 * 1024

# Below this size a multi-connection download is not worth the extra round trips,
# so each segment must be at least this large or we fall back to a single stream.
_MIN_SPLIT = 8 * 1024 * 1024


def download_to_file(
    url: str,
    dest_path: str,
    *,
    desc: Optional[str] = None,
    timeout: int = 600,
    headers: Optional[dict[str, str]] = None,
    connections: int = 1,
) -> str:
    """Stream ``url`` to ``dest_path`` atomically, with a byte-level progress bar.

    Writes to ``<dest_path>.part`` and renames on success, so an interrupted
    download never leaves a half-written file in place. The bar tracks transferred
    bytes against the response ``Content-Length`` (indeterminate if absent) and
    auto-hides on a non-TTY stderr.

    Args:
        url: Source URL.
        dest_path: Final destination path (its parent dir is created).
        desc: Short bar label (defaults to the destination basename).
        timeout: Per-request socket timeout in seconds.
        headers: Extra request headers (a default User-Agent is always sent).
        connections: Number of parallel HTTP range connections to use. ``>1``
            splits the file into that many byte segments fetched concurrently,
            which bypasses per-connection server throttling (e.g. Zenodo). Falls
            back to a single stream when the server does not honour range requests,
            the size is unknown, or the file is too small to split (see
            ``_MIN_SPLIT``).

    Returns:
        ``dest_path``.
    """
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    req_headers = {"User-Agent": _USER_AGENT}
    if headers:
        req_headers.update(headers)
    label = desc or os.path.basename(dest_path)
    tmp = f"{dest_path}.part"
    try:
        if connections > 1 and _parallel_download(
                url, tmp, req_headers, connections, timeout, label):
            pass  # tmp now holds the complete file
        else:
            _single_download(url, tmp, req_headers, timeout, label)
        os.replace(tmp, dest_path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return dest_path


def _single_download(
    url: str, tmp: str, req_headers: dict[str, str], timeout: int, label: str,
) -> None:
    """Stream the whole body over one connection into ``tmp``."""
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        total = int(resp.headers.get("Content-Length") or 0) or None
        with open(tmp, "wb") as fh, tqdm(
            total=total, desc=label,
            unit="B", unit_scale=True, unit_divisor=1024,
            disable=None, leave=False,
        ) as bar:
            while True:
                block = resp.read(_CHUNK)
                if not block:
                    break
                fh.write(block)
                bar.update(len(block))


def _parallel_download(
    url: str, tmp: str, req_headers: dict[str, str],
    connections: int, timeout: int, label: str,
) -> bool:
    """Fetch ``url`` into ``tmp`` over ``connections`` concurrent range requests.

    Returns ``True`` once ``tmp`` holds the complete file, or ``False`` if the
    server does not support ranges / hides the size / the file is too small --
    signalling the caller to retry over a single stream. A network failure mid
    transfer is raised so the caller's existing fail-soft handling applies.
    """
    total = _probe_size(url, req_headers, timeout)
    if total is None or total < connections * _MIN_SPLIT:
        return False

    seg = total // connections
    ranges = [
        (i * seg, (total - 1 if i == connections - 1 else (i + 1) * seg - 1))
        for i in range(connections)
    ]
    with open(tmp, "wb") as presize:  # pre-size so every segment can seek to its offset
        presize.truncate(total)

    errors: list[Exception] = []
    write_lock = threading.Lock()
    bar_lock = threading.Lock()

    with open(tmp, "r+b") as fh, tqdm(
        total=total, desc=label,
        unit="B", unit_scale=True, unit_divisor=1024,
        disable=None, leave=False,
    ) as bar:
        def worker(start: int, end: int) -> None:
            try:
                h = dict(req_headers)
                h["Range"] = f"bytes={start}-{end}"
                req = urllib.request.Request(url, headers=h)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    pos = start
                    while True:
                        block = resp.read(_CHUNK)
                        if not block:
                            break
                        with write_lock:  # serialise the seek+write pair only
                            fh.seek(pos)
                            fh.write(block)
                        pos += len(block)
                        with bar_lock:
                            bar.update(len(block))
            except Exception as exc:  # pragma: no cover - network dependent
                errors.append(exc)

        workers = [
            threading.Thread(target=worker, args=(s, e), daemon=True)
            for s, e in ranges
        ]
        for t in workers:
            t.start()
        for t in workers:
            t.join()

    if errors:
        raise errors[0]
    return True


def _probe_size(
    url: str, req_headers: dict[str, str], timeout: int,
) -> Optional[int]:
    """Return the total byte size iff the server honours range requests, else ``None``.

    Sends a one-byte range request: a ``206`` with a ``Content-Range`` total means
    ranges work (and gives the size); anything else (``200``, error, missing total)
    means we should not attempt a segmented download.
    """
    h = dict(req_headers)
    h["Range"] = "bytes=0-0"
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 206:
                return None
            content_range = resp.headers.get("Content-Range") or ""
    except urllib.error.URLError:
        return None
    if "/" not in content_range:
        return None
    try:
        return int(content_range.rsplit("/", 1)[1])
    except ValueError:
        return None


def extract_zip_with_progress(
    zf: zipfile.ZipFile,
    dest_dir: str,
    *,
    members: Optional[list[str]] = None,
    desc: str = "Extracting",
) -> None:
    """Extract ``members`` (default: all) from an open zip with a byte-level bar.

    A drop-in, progress-reporting replacement for ``ZipFile.extractall`` for the
    large archives FireDataForge unpacks. The bar tracks uncompressed bytes and
    auto-hides on a non-TTY stderr.
    """
    infos = (
        [zf.getinfo(name) for name in members]
        if members is not None else zf.infolist()
    )
    os.makedirs(dest_dir, exist_ok=True)
    total = sum(info.file_size for info in infos) or None
    with tqdm(
        total=total, desc=desc, unit="B", unit_scale=True, unit_divisor=1024,
        disable=None, leave=False,
    ) as bar:
        for info in infos:
            zf.extract(info, dest_dir)
            bar.update(info.file_size)
