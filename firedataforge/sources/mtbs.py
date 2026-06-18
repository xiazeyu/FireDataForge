"""MTBS / Provisional IA fire-metadata access (USFS EDW service)."""

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime
from typing import Iterator, Optional
from urllib.parse import urlencode

log = logging.getLogger(__name__)


MTBS_REST_URL = (
    "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_MTBS_01/MapServer/63/query"
)

# Provisional Initial Assessment supplement: recent fires not yet queryable from
# the EDW service. ``bounds`` is (minx, miny, maxx, maxy) in lon/lat (EPSG:4326);
# ``t_start`` is the ignition date (MTBS provides no end date).
PROVISIONAL_IA: dict[str, dict] = {
    "CA3406811855120250107": {  # Palisades Fire, Los Angeles County, Jan 2025
        "name": "PALISADES",
        "year": 2025,
        "acres": 23448,
        "t_start": datetime(2025, 1, 7),
        "fire_type": "Wildfire",
        "asmnt_type": "Provisional IA",
        "bounds": (-118.69361, 34.02671, -118.48883, 34.14177),
    },
    "CA3419211810520250108": {  # Eaton Fire, Los Angeles County, Jan 2025
        "name": "EATON",
        "year": 2025,
        "acres": 14021,
        "t_start": datetime(2025, 1, 7),
        "fire_type": "Wildfire",
        "asmnt_type": "Provisional IA",
        "bounds": (-118.16313, 34.16111, -118.01330, 34.23908),
    },
}

# Attributes requested from the MTBS service (shared by single-event + bulk paths).
_MTBS_OUT_FIELDS = (
    "fire_id,fire_name,year,acres,ig_date,startmonth,startday,"
    "fire_type,asmnt_type,irwinid,map_id"
)


def _ignition_date(attrs: dict) -> Optional[datetime]:
    """Parse the ignition (start) date from MTBS attributes; ``None`` if absent."""
    ig = attrs.get("ig_date")
    if ig:
        digits = str(int(ig))
        if len(digits) == 8:
            try:
                return datetime(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
            except ValueError:
                pass
    year, month, day = attrs.get("year"), attrs.get("startmonth"), attrs.get("startday")
    if year and month and day:
        try:
            return datetime(int(year), int(month), int(day))
        except ValueError:
            pass
    return None


def _bbox_from_rings(rings: list) -> Optional[tuple[float, float, float, float]]:
    """Return (minx, miny, maxx, maxy) from Esri polygon rings, or None if empty."""
    points = [pt for ring in rings for pt in ring]
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _record_from_feature(feature: dict, fallback_id: Optional[str]) -> Optional[dict]:
    """Convert an Esri feature into a metadata record, or None if it has no geometry."""
    attrs = feature.get("attributes", {})
    bounds = _bbox_from_rings(feature.get("geometry", {}).get("rings") or [])
    if bounds is None:
        return None
    event_id = attrs.get("fire_id") or fallback_id
    return {
        "event_id": event_id,
        "name": attrs.get("fire_name") or event_id,
        "year": int(attrs.get("year") or int(str(event_id)[-8:-4])),
        "acres": int(round(attrs.get("acres") or 0)),
        "t_start": _ignition_date(attrs),
        "fire_type": attrs.get("fire_type") or "",
        "asmnt_type": attrs.get("asmnt_type") or "",
        "irwinid": attrs.get("irwinid") or "",
        "map_id": attrs.get("map_id") or "",
        "bounds": bounds,
    }


def query_mtbs(event_id: str, include_provisional: bool = True) -> Optional[dict]:
    """Resolve one Event ID from the live MTBS service (+ Provisional IA supplement).

    Returns a metadata record (see :func:`_record_from_feature`) or ``None`` if
    the event is available from neither source (service down or fire not released).
    """
    query = urlencode({
        "where": f"fire_id='{event_id}'",
        "outFields": _MTBS_OUT_FIELDS,
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "json",
    })
    try:
        with urllib.request.urlopen(f"{MTBS_REST_URL}?{query}", timeout=30) as resp:
            payload = json.loads(resp.read().decode())
        features = payload.get("features") or []
        if features:
            record = _record_from_feature(features[0], event_id)
            if record is not None:
                return record
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        log.warning(f"MTBS service unavailable for {event_id}: {exc}")

    if include_provisional and event_id in PROVISIONAL_IA:
        log.info(f"{event_id} resolved from the Provisional IA supplement")
        return {"event_id": event_id, **PROVISIONAL_IA[event_id]}
    return None


def iter_all_events(year_min: Optional[int] = None, page_size: int = 2000) -> Iterator[dict]:
    """Page through every MTBS burned-area boundary, yielding metadata records.

    Geometry is simplified server-side so each polygon collapses to a handful of
    points -- enough for an accurate bounding box while keeping payloads small.
    The Provisional IA supplement is merged separately by :func:`build_firelist`.
    """
    where = "1=1" if year_min is None else f"year>={int(year_min)}"
    offset = 0
    while True:
        query = urlencode({
            "where": where,
            "outFields": _MTBS_OUT_FIELDS,
            "returnGeometry": "true",
            "outSR": "4326",
            "maxAllowableOffset": 1000,
            "geometryPrecision": 5,
            "resultOffset": offset,
            "resultRecordCount": page_size,
            "f": "json",
        })
        with urllib.request.urlopen(f"{MTBS_REST_URL}?{query}", timeout=120) as resp:
            payload = json.loads(resp.read().decode())
        features = payload.get("features") or []
        if not features:
            break
        for feature in features:
            record = _record_from_feature(feature, None)
            if record is not None:
                yield record
        if len(features) < page_size and not payload.get("exceededTransferLimit"):
            break
        offset += len(features)
