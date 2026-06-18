"""Credentials, the first-run setup wizard, and dataset/credential discovery."""

import glob
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from typing import Optional

from firedataforge.constants import (
    DEFAULT_FIRELIST_CACHE, FEDS_CACHE_DIR, FEDS_DIR,
    FEDS_MTBS_FIRELIST_NAME, FEDS_MTBS_ZENODO_URL, FEDS_MTBS_ZIP_NAME,
)

log = logging.getLogger(__name__)


# Persist settings at the repo root (next to ``main.py``), one directory up from
# this package, so ``.env`` sits where users expect it.
ENV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
)

# Which credential / dependency unlocks which output features, so the wizard can
# tell the user exactly what skipping a step would cost. Keep in sync with the
# layer registry in ``process_single_fire`` and the README feature table.
GEE_FEATURES = [
    "elevation", "terrain_rgb", "canopy_bulk_density", "canopy_cover",
    "building_height", "landcover", "lai", "sentinel2_rgb",
]
FIRMS_FEATURES = ["frp_daytime", "frp_nighttime"]
FEDS_FEATURES = ["burn_perimeter", "fireline", "fireline_max_frp"]
# Features that need no credentials at all (shown for reassurance).
FREE_FEATURES = ["wui", "recent_burn", "r2", "u10", "v10"]


def _parse_env_file(path: str) -> dict[str, str]:
    """Parse a ``.env`` file into a dict of KEY -> value (last wins)."""
    data: dict[str, str] = {}
    if not os.path.exists(path):
        return data
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                data[key] = value
    return data


def load_env(path: str = ENV_PATH) -> None:
    """Load values from ``path`` into ``os.environ`` without overriding existing.

    Safe to call multiple times and at import; the real environment always takes
    precedence over the persisted file.
    """
    for key, value in _parse_env_file(path).items():
        os.environ.setdefault(key, value)


def ensure_ca_bundle() -> None:
    """Point OpenSSL/requests at certifi's CA bundle when the system store is unusable.

    Some Python builds (e.g. uv-managed CPython on HPC nodes) ship no CA bundle,
    so OpenSSL's default ``cafile`` is missing and every HTTPS call fails with
    ``CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate``. When
    the user has not already chosen a bundle and the default one is absent, fall
    back to the certifi bundle that ships with our dependencies. Exported via the
    environment so child processes (notably ``earthengine authenticate``) inherit it.
    """
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return  # respect an explicit user choice
    import ssl

    default_cafile = ssl.get_default_verify_paths().cafile
    if default_cafile and os.path.exists(default_cafile):
        return  # the system store already works
    try:
        import certifi
    except ImportError:
        return
    bundle = certifi.where()
    if not os.path.exists(bundle):
        return
    os.environ["SSL_CERT_FILE"] = bundle
    os.environ.setdefault("REQUESTS_CA_BUNDLE", bundle)


def set_env_var(key: str, value: str, path: str = ENV_PATH) -> None:
    """Upsert ``key=value`` into the ``.env`` file and the live environment."""
    lines: list[str] = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()

    out: list[str] = []
    found = False
    for line in lines:
        stripped = line.strip()
        is_assignment = (
            stripped and not stripped.startswith("#") and "=" in stripped
            and stripped.split("=", 1)[0].strip() == key
        )
        if is_assignment:
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out).rstrip("\n") + "\n")
    os.environ[key] = value


def is_first_run(path: str = ENV_PATH) -> bool:
    """First run if no ``.env`` file exists yet."""
    return not os.path.exists(path)


def is_interactive() -> bool:
    """True when both stdin and stdout are attached to a terminal."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _prompt(message: str, default: str = "") -> str:
    try:
        answer = input(message).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return answer or default


def _yes_no(message: str, default: bool = True) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    answer = _prompt(message + suffix).lower()
    if not answer:
        return default
    return answer.startswith("y")


def gee_ready(project: Optional[str] = None) -> bool:
    """Return True if Earth Engine can initialize and answer a trivial query now."""
    import ee  # imported lazily so this module stays light for metadata-only use
    project = project or os.environ.get("EARTHENGINE_PROJECT")
    try:
        if project:
            ee.Initialize(project=project)
        else:
            ee.Initialize()
        ee.Number(1).getInfo()
        return True
    except Exception:
        return False


def firms_key_valid(map_key: str) -> Optional[bool]:
    """Best-effort online check of a FIRMS MAP_KEY.

    Returns True/False when it could be determined, or None if the check itself
    failed (e.g. no network) so the caller can treat it as "unknown".
    """
    url = f"https://firms.modaps.eosdis.nasa.gov/mapserver/mapkey_status/?MAP_KEY={map_key}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace").lower()
    except urllib.error.HTTPError:
        return False
    except Exception:
        return None
    if "invalid" in body or "error" in body:
        return False
    return "current_transactions" in body or "transaction" in body or "map_key" in body


def find_feds_firelist(feds_dir: str = FEDS_DIR) -> Optional[str]:
    """Locate the FEDS-MTBS fire list in ``feds_dir``, if present.

    Matches, in order of preference:

    * the bundled example list ``fireslist_examples.csv`` shipped by
      ``--fetch-examples`` (FEDS perimeter bbox plus explicit ``tst``/``ted`` for
      the eight demo fires), then
    * the released summary GeoPackage ``fireslist_FEDS25MTBS_2012-2024.geojson``
      (MTBS final perimeters + metadata for all 7,739 fires).

    Any top-level ``*fire*list*`` file with a ``.geojson``, ``.gpkg`` or ``.csv``
    extension is accepted case-insensitively, so a spelling difference never
    disables this offline metadata source. The example list wins when present
    (it carries the demo fires' exact end dates); otherwise GeoPackage/GeoJSON
    lists win over a generic CSV.
    """
    if not os.path.isdir(feds_dir):
        return None
    candidates = []
    for ext in ("geojson", "gpkg", "csv"):
        candidates += [
            f for f in glob.glob(os.path.join(feds_dir, f"*.{ext}"))
            if "list" in os.path.basename(f).lower()
            and "fire" in os.path.basename(f).lower()
        ]
    # The bundled example list wins outright when present; otherwise prefer the
    # released GeoPackage list (.geojson/.gpkg) over a CSV, and within a kind
    # prefer a name containing the full word "firelist"/"fireslist".
    ext_rank = {".geojson": 0, ".gpkg": 0, ".csv": 1}

    def _key(f: str) -> tuple:
        base = os.path.basename(f).lower()
        _, ext = os.path.splitext(base)
        not_example = "example" not in base  # examples list first (False < True)
        named = "firelist" not in base and "fireslist" not in base
        return (not_example, ext_rank.get(ext, 2), named, base)

    candidates.sort(key=_key)
    return candidates[0] if candidates else None


def count_feds_gpkgs(base_dir: str = FEDS_DIR) -> int:
    """Count FEDS perimeter GeoPackages available anywhere under ``base_dir``."""
    if not os.path.isdir(base_dir):
        return 0
    return sum(len(files) for files in (
        [f for f in fs if f.lower().endswith(".gpkg")]
        for _, _, fs in os.walk(base_dir)
    ))


def feds_available() -> bool:
    """True when FEDS25MTBS data (fire list or GeoPackages) is present locally.

    Checks both the user archive (``datasets/``) and the software cache
    (``cache/``), so a run that has already lazily fetched fires still counts.
    """
    return (find_feds_firelist() is not None
            or count_feds_gpkgs(FEDS_DIR) > 0
            or count_feds_gpkgs(FEDS_CACHE_DIR) > 0)


def _setup_gee(path: str) -> None:
    print("\n[1/5] Google Earth Engine")
    print("      Unlocks: " + ", ".join(GEE_FEATURES))
    print("      Get access: enable Earth Engine on a Google Cloud project at "
          "https://console.cloud.google.com/")
    if gee_ready():
        print("      OK  Already authenticated and initialized.")
        return
    if not _yes_no("      Configure Earth Engine now?"):
        print("      --  Skipped. Disabled features: " + ", ".join(GEE_FEATURES))
        return
    print("      Launching 'earthengine authenticate'...")
    print("      A URL will be printed; open it, authorize, and paste the code back.")
    try:
        # auth_mode=notebook prints a URL + accepts a pasted verification code,
        # so it works on headless/HPC nodes with no browser and no gcloud CLI.
        subprocess.run(
            ["earthengine", "authenticate", "--auth_mode=notebook"], check=False
        )
    except FileNotFoundError:
        print("      !  'earthengine' CLI not found. Install with: pip install earthengine-api")
    print("      To get a project ID:")
    print("        1. Go to Google Cloud Console: https://console.cloud.google.com/")
    print("        2. Create or select a project with Earth Engine enabled:")
    print("           https://developers.google.com/earth-engine/guides/access")
    print("        3. Copy the project ID")
    project = _prompt("      Google Cloud project ID (blank to skip): ")
    if project:
        try:
            subprocess.run(["earthengine", "set_project", project], check=False)
        except FileNotFoundError:
            pass
        set_env_var("EARTHENGINE_PROJECT", project, path)
    if gee_ready(project or None):
        print("      OK  Earth Engine is ready.")
    else:
        print("      !  Earth Engine still not initialized; GEE features will be "
              "skipped until setup is finished (rerun: python main.py --setup).")


def _setup_firms(path: str) -> None:
    print("\n[2/5] NASA FIRMS map key  (VIIRS active fire / FRP)")
    print("      Unlocks: " + ", ".join(FIRMS_FEATURES))
    print("      Get access (free, instant): https://firms.modaps.eosdis.nasa.gov/api/map_key/")
    print("      Each fire's FRP is streamed from the Area API and cached per")
    print("      event under cache/FIRMS/ (no bulk download). To work fully")
    print("      offline instead, drop archive CSVs into datasets/FIRMS/.")
    if _firms_map_key():
        print("      OK  A FIRMS key is already configured.")
        return
    key = _prompt("      Paste your FIRMS MAP_KEY (blank to skip): ")
    if not key:
        print("      --  Skipped. FRP needs either a FIRMS key or the local FEDS "
              "firepix archive (pre-2025).")
        return
    valid = firms_key_valid(key)
    if valid is False:
        if not _yes_no("      !  That key looks invalid. Save it anyway?", default=False):
            print("      Skipped.")
            return
    elif valid is None:
        print("      (could not verify the key online -- saving it as given)")
    else:
        print("      OK  Key verified.")
    set_env_var("FIRMS_MAP_KEY", key, path)


def _setup_feds() -> None:
    print("\n[3/5] FEDS-MTBS archive  (Zenodo: Chen et al. -- perimeter GeoPackages")
    print("      + firepix; geometry, active-fire window, and bounds per fire)")
    print("      Unlocks: " + ", ".join(FEDS_FEATURES) + " (and perimeter-masked FRP)")
    print(f"      Full dataset (7,739 fires, 2012-2024): {FEDS_MTBS_ZENODO_URL}")
    print("      (Fire NAME + acreage are configured separately in step [5/5].)")
    firelist = find_feds_firelist()
    n_gpkg = count_feds_gpkgs(FEDS_DIR)
    if firelist or n_gpkg:
        print(f"      OK  Found a local FEDS archive in {FEDS_DIR} "
              f"(fire list: {'yes' if firelist else 'no'}, {n_gpkg} GeoPackage(s)).")
        return
    print("      Choose how to get the data:")
    print(f"       - Manual        : unzip {FEDS_MTBS_ZIP_NAME} into {FEDS_DIR}/")
    print("       - Full download : zip (~370 MB) into datasets/  (saves bandwidth")
    print("                         on repeated runs; all fires resolve offline)")
    print("       - On-the-fly    : each requested fire is range-pulled from")
    print("                         Zenodo into cache/ as needed (saves disk)")
    if _yes_no("      Download the full FEDS-MTBS archive (zip) now?", default=False):
        # Imported lazily to avoid a config -> remote_archive import at load.
        from firedataforge.remote_archive import download_full_feds_archive
        try:
            download_full_feds_archive()  # zip only; fire list is step [5/5]
            print(f"      OK  Full FEDS-MTBS archive unpacked into {FEDS_DIR}.")
            return
        except Exception as exc:  # pragma: no cover - network dependent
            print(f"      !  Could not download the full archive ({exc}); "
                  "falling back to on-the-fly fetching.")
    else:
        print(f"      --  On-the-fly: fires stream into {FEDS_CACHE_DIR}/ as needed.")
    # On-the-fly chosen (or full download failed): offer the tiny example bundle
    # so the demos/benchmark/validation can run without any large download.
    from firedataforge.examples import examples_record_configured, fetch_examples
    if examples_record_configured() and _yes_no(
            "      Also download the 8 example fires now (~22 MB, lets the demos run)?"):
        try:
            fetch_examples()
            print(f"      OK  Example fires downloaded to {FEDS_DIR}.")
        except Exception as exc:  # pragma: no cover - network dependent
            print(f"      !  Could not fetch examples ({exc}); skipping.")


def _setup_globalwui() -> None:
    print("\n[4/5] Global WUI  (wildland-urban interface)")
    print("      Unlocks: wui")
    # Imported lazily: wui pulls in rioxarray/rasterio, heavy for metadata-only use.
    from firedataforge.sources.wui import (
        DEFAULT_GLOBALWUI_DIR, download_globalwui_archive,
    )
    if os.path.isdir(DEFAULT_GLOBALWUI_DIR) and any(
            f.endswith(".tif")
            for _, _, fs in os.walk(DEFAULT_GLOBALWUI_DIR) for f in fs):
        print(f"      OK  Found a local Global WUI archive in {DEFAULT_GLOBALWUI_DIR}.")
        return
    print("      Choose how to get the data:")
    print("       - Full download : ~3.8 GB North America archive into datasets/")
    print("       - On-the-fly    : only the ~32 KB tiles each fire needs are")
    print("                         streamed into cache/ (recommended)")
    if _yes_no("      Download the full Global WUI archive now?", default=False):
        try:
            download_globalwui_archive()
            print(f"      OK  Full Global WUI archive unpacked into {DEFAULT_GLOBALWUI_DIR}.")
        except Exception as exc:  # pragma: no cover - network dependent
            print(f"      !  Could not download the full archive ({exc}); "
                  "tiles will stream on demand instead.")
    else:
        print("      --  On-the-fly: tiles stream into cache/GlobalWUI/ as needed.")


def _setup_fire_metadata(path: str) -> None:
    """Let the user choose where the fire NAME + acreage come from.

    The GeoPackage of step [3/5] already supplies each fire's geometry, active-fire
    window, and bounds; only the human-readable name + acreage are missing, and the
    three sources trade off accuracy, recency, and reliability differently. The
    pipeline always prefers, in order, whatever is present: FEDS-MTBS fire list >
    MTBS fire list > live mtbs.gov > the Event ID. This step just stages the source
    the user prefers.
    """
    print("\n[5/5] Fire name & acreage  (event metadata only -- geometry/window/")
    print("      bounds already come from the FEDS GeoPackage)")
    feds_fl = find_feds_firelist()
    mtbs_fl = os.path.exists(DEFAULT_FIRELIST_CACHE)
    if feds_fl or mtbs_fl:
        have = []
        if feds_fl:
            have.append(f"FEDS-MTBS fire list ({os.path.basename(feds_fl)})")
        if mtbs_fl:
            have.append(f"MTBS fire list ({DEFAULT_FIRELIST_CACHE})")
        print(f"      OK  Already available: {', '.join(have)}.")
        return
    print("      Pick a source (all are optional; the gpkg still drives the data):")
    print(f"       1) FEDS-MTBS fire list  -- {FEDS_MTBS_FIRELIST_NAME} from Zenodo")
    print("                                  (~280 MB). Acreage aligned to FEDS;")
    print("                                  2012-2024 only; offline & most reliable.")
    print("       2) MTBS fire list        -- built from mtbs.gov (~30k fires, ~30s).")
    print("                                  More recent coverage; acreage NOT FEDS-")
    print("                                  aligned; offline once built.")
    print("       3) On-the-fly (mtbs.gov) -- nothing staged; resolved live per fire.")
    print("                                  Most up-to-date, but per-event network")
    print("                                  and least reliable.")
    choice = _prompt("      Choose 1/2/3 [3]: ", default="3").strip()
    if choice == "1":
        from firedataforge.remote_archive import download_feds_firelist
        try:
            dest = download_feds_firelist(FEDS_DIR)
            if dest:
                print(f"      OK  FEDS-MTBS fire list saved to {dest}")
            else:
                print("      !  Could not download the FEDS-MTBS fire list; "
                      "names will fall back to mtbs.gov / the Event ID.")
        except Exception as exc:  # pragma: no cover - network dependent
            print(f"      !  Could not download the FEDS-MTBS fire list ({exc}); skipping.")
    elif choice == "2":
        # Imported lazily to avoid a config -> events import cycle at module load.
        from firedataforge.events import build_firelist
        try:
            build_firelist(DEFAULT_FIRELIST_CACHE)
            print(f"      OK  Saved offline MTBS fire list to {DEFAULT_FIRELIST_CACHE}")
        except Exception as exc:  # pragma: no cover - network dependent
            print(f"      !  Could not build the MTBS fire list ({exc}); skipping.")
    else:
        print("      --  On-the-fly: names/acreage resolved live from mtbs.gov, "
              "falling back to the Event ID. Stage a list later with "
              "`python main.py --build-firelist`.")


def run_setup_wizard(path: str = ENV_PATH) -> None:
    """Run the interactive first-run setup and persist results to ``.env``."""
    print("=" * 70)
    print("  FireDataForge -- first-run setup")
    print("=" * 70)
    print("All steps are optional; skip any and rerun later with:  python main.py --setup")
    print("Features needing no setup: " + ", ".join(FREE_FEATURES))

    _setup_gee(path)
    _setup_firms(path)
    _setup_feds()
    _setup_globalwui()
    _setup_fire_metadata(path)

    # Write the marker so .env exists and we don't nag on the next run.
    set_env_var("FIREDATAFORGE_SETUP", datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), path)
    print("\nSettings saved to .env (gitignored). Setup complete.\n")


def ensure_setup(interactive: Optional[bool] = None, path: str = ENV_PATH) -> None:
    """Load persisted settings and run the wizard on the first interactive run."""
    if interactive is None:
        interactive = is_interactive()
    if is_first_run(path) and interactive:
        run_setup_wizard(path)
    load_env(path)


def _firms_map_key() -> Optional[str]:
    """Return the NASA FIRMS MAP_KEY from the environment, if configured."""
    key = os.environ.get("FIRMS_MAP_KEY") or os.environ.get("MAP_KEY")
    return key.strip() if key else None
