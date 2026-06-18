"""Run the FireDataForge validation metrics across one or more processed events.

Usage:
    python validation/run_validation.py --output-dir output --events events.txt
    python validation/run_validation.py --output-dir output CA3432611848120191010 ...

Writes ``<output-dir>/validation_metrics.csv`` (one row per event) and prints a
per-event summary. See ``validation/metrics.py`` for the metric definitions; the
categorical/continuous metrics re-fetch a native-resolution reference from Earth
Engine, so they need a configured GEE project and are skipped offline.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys

# Allow running as `python validation/run_validation.py` from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from validation import metrics  # noqa: E402


def _event_ids(args) -> list[str]:
    ids: list[str] = list(args.events_pos)
    if args.events and os.path.isfile(args.events):
        with open(args.events) as fh:
            ids += [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
    elif args.events:
        ids += [e.strip() for e in args.events.split(",") if e.strip()]
    # De-duplicate, preserve order.
    seen, out = set(), []
    for e in ids:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("events_pos", nargs="*", metavar="EVENT_ID",
                        help="Event IDs to validate")
    parser.add_argument("--events", help="File of event IDs or a comma-separated list")
    parser.add_argument("--output-dir", default="output",
                        help="Directory containing <event_id>/ output folders")
    args = parser.parse_args()

    event_ids = _event_ids(args)
    if not event_ids:
        parser.error("provide event IDs positionally or via --events")

    rows: list[dict] = []
    for event_id in event_ids:
        event_dir = os.path.join(args.output_dir, event_id)
        if not os.path.isdir(event_dir):
            print(f"[skip] {event_id}: no output directory at {event_dir}")
            continue
        row: dict = {"event_id": event_id}
        for name, fn in metrics.ALL_METRICS.items():
            try:
                result = fn(event_dir)
            except Exception as exc:  # keep the batch going
                result = {"error": repr(exc)}
            for k, v in result.items():
                row[f"{name}.{k}"] = v
        rows.append(row)
        print(f"[ok]   {event_id}: "
              + ", ".join(f"{k}={v}" for k, v in row.items() if k != "event_id"))

    if not rows:
        print("No events validated.")
        return
    fields = sorted({k for row in rows for k in row})
    fields.remove("event_id")
    fields = ["event_id"] + fields
    out_csv = os.path.join(args.output_dir, "validation_metrics.csv")
    with open(out_csv, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nWrote {len(rows)} row(s) to {out_csv}")


if __name__ == "__main__":
    main()
