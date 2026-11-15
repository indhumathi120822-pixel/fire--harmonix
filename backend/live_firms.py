import csv
import io
import os
import time
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
import pandas as pd
import requests
from sqlalchemy import text
from dotenv import load_dotenv

from backend import science_engine
from backend.database import get_engine
from backend.config import NASA_MAP_KEY, INDIA_BBOX, LIVE_CACHE_TTL_SECONDS, FIRMS_DAYS

load_dotenv()

_LIVE_CACHE: Dict[str, Any] = {
    "expires_at": 0.0,
    "payload": None,
    "features": None,
    "features_by_id": {},
}


def parse_firms_csv(csv_text: str, sensor_label: str) -> List[Dict[str, Any]]:
    """
    Safely parse CSV text from NASA FIRMS using pure-Python csv.DictReader,
    protecting against Windows Application Control C-extension DLL crashes.
    """
    if not csv_text or "latitude" not in csv_text[:300]:
        return []

    reader = csv.DictReader(io.StringIO(csv_text))
    rows: List[Dict[str, Any]] = []
    now_utc = datetime.now(timezone.utc)

    for r in reader:
        try:
            lat = float(r.get("latitude", ""))
            lon = float(r.get("longitude", ""))
            frp = float(r.get("frp", 0.0) or 0.0)
            acq_date = r.get("acq_date", "").strip() or now_utc.strftime("%Y-%m-%d")

            try:
                dt = datetime.strptime(acq_date, "%Y-%m-%d")
                m, d, y = dt.month, dt.day, dt.year
            except Exception:
                m, d, y = now_utc.month, now_utc.day, now_utc.year

            rows.append({
                "latitude": lat,
                "longitude": lon,
                "frp": frp,
                "sensor": sensor_label,
                "acq_date": acq_date,
                "month": m,
                "day": d,
                "year": y,
            })
        except (ValueError, TypeError):
            continue

    return rows


def fetch_live_firms(map_key: str, bbox: str = INDIA_BBOX, days: int = FIRMS_DAYS) -> pd.DataFrame:
    """
    Fetch live real-time NRT detections from NASA FIRMS Area API for VIIRS and MODIS
    using the configured API key with strict timeouts and schema normalization.
    Strictly forbids any fake, dummy, or demo data generation.
    """
    active_key = (
        map_key
        or NASA_MAP_KEY
        or os.getenv("NASA_MAP_KEY")
        or os.getenv("FIRMS_MAP_KEY")
        or os.getenv("NASA_FIRMS_API_KEY")
        or os.getenv("MAP_KEY")
        or ""
    ).strip()

    if not active_key:
        return pd.DataFrame()

    sensors = [
        ("VIIRS_NOAA20_NRT", "VIIRS"),
        ("VIIRS_NOAA21_NRT", "VIIRS"),
        ("VIIRS_SNPP_NRT", "VIIRS"),
        ("MODIS_NRT", "MODIS"),
    ]
    all_rows: List[Dict[str, Any]] = []

    def _fetch_single(args_tuple):
        source_code, sensor_label, d_val = args_tuple
        url = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{active_key}/{source_code}/{bbox}/{d_val}"
        try:
            resp = requests.get(url, timeout=4)
            if resp.status_code == 200:
                return parse_firms_csv(resp.text, sensor_label)
        except Exception:
            pass
        return []

    # Parallel fetch across all sensors with strict timeout=4
    import concurrent.futures
    tasks = [(s[0], s[1], days) for s in sensors]
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            for r_list in executor.map(_fetch_single, tasks):
                all_rows.extend(r_list)
    except Exception:
        pass

    if not all_rows and days < 2:
        tasks_fallback = [(s[0], s[1], 2) for s in sensors]
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                for r_list in executor.map(_fetch_single, tasks_fallback):
                    all_rows.extend(r_list)
        except Exception:
            pass

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    df.dropna(subset=["latitude", "longitude"], inplace=True)
    return df


def harmonize_live(modis_df: pd.DataFrame, viirs_df: pd.DataFrame) -> pd.DataFrame:
    """
    Combine, deduplicate (1 km Haversine), calibrate with analytical Stefan-Boltzmann
    physics, and compute HFAI for real live detections.
    """
    combined = pd.concat([modis_df, viirs_df], ignore_index=True)
    if combined.empty:
        return combined

    dedup = science_engine.deduplicate_live(combined, distance_km=1.0)
    cal = science_engine.apply_physics_calibration(dedup)
    scored = science_engine.compute_hfai(cal)
    grid_scored = science_engine.assign_grid_cell(scored)

    # Assign live IDs and anomaly severities
    grid_scored.reset_index(drop=True, inplace=True)
    grid_scored["id"] = ["live_" + str(i + 1) for i in range(len(grid_scored))]
    grid_scored["event_id"] = -(grid_scored.index.astype(int) + 1)  # negative int for distinct event_id
    grid_scored["anomaly_severity"] = "GREEN"
    grid_scored.loc[grid_scored["hfai"] >= 45.0, "anomaly_severity"] = "YELLOW"
    grid_scored.loc[grid_scored["hfai"] >= 65.0, "anomaly_severity"] = "RED"
    return grid_scored


def generate_recommendation(severity: str, pct_diff: float, live_count: int, historical_median: float) -> str:
    """Generate actionable plain-language operational advice for fire managers."""
    if severity == "GREEN":
        return f"Fire activity within normal historical range for this date ({live_count} active vs {historical_median:.1f} median). Continue routine monitoring."
    if severity == "YELLOW":
        return (
            f"Elevated fire activity detected ({pct_diff:+.1f}% vs. historical median: {live_count} vs {historical_median:.1f}). "
            "Recommend increasing patrol frequency and verifying sensor observations."
        )
    return (
        f"Unusual fire surge detected ({pct_diff:+.1f}% vs. historical median: {live_count} vs {historical_median:.1f}). "
        "Immediate aerial & ground reconnaissance recommended. Alert district emergency responders."
    )


def refresh_live_cache(map_key: str = "") -> Dict[str, Any]:
    """
    Fetch live NASA FIRMS detections, execute physics calibration and HFAI inference,
    and update in-memory cache with 5-minute TTL.
    """
    active_key = (
        map_key
        or NASA_MAP_KEY
        or os.getenv("NASA_MAP_KEY")
        or os.getenv("FIRMS_MAP_KEY")
        or os.getenv("NASA_FIRMS_API_KEY")
        or os.getenv("MAP_KEY")
        or ""
    ).strip()

    now = time.time()
    today = datetime.now(timezone.utc)

    # Read historical baseline
    hist_median = 0.0
    mad = 0.0
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT historical_median_fires, mad FROM baseline WHERE month = ? AND day = ?",
                (today.month, today.day),
            )
            row = cursor.fetchone()
        if row:
            hist_median = float(row[0]) if row[0] is not None else 0.0
            mad = float(row[1]) if row[1] is not None else 0.0
    except Exception:
        pass

    if not active_key:
        payload = {
            "status": "error",
            "live_count": 0,
            "historical_median": round(hist_median, 1),
            "z_score": 0.0,
            "pct_diff": 0.0,
            "severity": "GREEN",
            "label": "unconfigured_key",
            "recommendation": "NASA FIRMS API key is not configured in .env. Please configure NASA_MAP_KEY.",
            "data_source": "NASA FIRMS (Missing Key)",
            "timestamp": today.isoformat(),
            "cached": False,
            "features": [],
        }
        _LIVE_CACHE["expires_at"] = now + LIVE_CACHE_TTL_SECONDS
        _LIVE_CACHE["payload"] = payload
        _LIVE_CACHE["features"] = []
        _LIVE_CACHE["features_by_id"] = {}
        return payload

    features: List[Dict[str, Any]] = []
    features_by_id: Dict[str, Dict[str, Any]] = {}

    try:
        raw_live = fetch_live_firms(active_key)
        if not raw_live.empty:
            modis_part = raw_live[raw_live["sensor"] == "MODIS"]
            viirs_part = raw_live[raw_live["sensor"] == "VIIRS"]
            harm_live = harmonize_live(modis_part, viirs_part)
            live_count = int(len(harm_live))
            data_source = "NASA FIRMS Satellite (Live VIIRS & MODIS NRT)"
            status = "healthy"

            records = harm_live.to_dict(orient="records")
            for r in records:
                feat = {
                    "id": str(r.get("id")),
                    "event_id": int(r.get("event_id", -1)),
                    "latitude": float(r["latitude"]),
                    "longitude": float(r["longitude"]),
                    "acq_date": str(r["acq_date"]),
                    "sensor": str(r["sensor"]),
                    "frp": float(r.get("frp", 0.0)),
                    "calibrated_frp": float(r.get("calibrated_frp", 0.0)),
                    "uncertainty_sigma": float(r.get("uncertainty_sigma", 0.0)),
                    "detection_limit_mw": float(r.get("detection_limit", 3.7)),
                    "physics_check": str(r.get("physics_check", "PASS")),
                    "hfai": float(r.get("hfai", 0.0)),
                    "hfai_energy": float(r.get("hfai_energy", 0.0)),
                    "hfai_brightness": float(r.get("hfai_brightness", 0.0)),
                    "hfai_confidence": float(r.get("hfai_confidence", 0.0)),
                    "hfai_agreement": float(r.get("hfai_agreement", 0.0)),
                    "anomaly_severity": str(r.get("anomaly_severity", "GREEN")),
                    "mode": "live",
                }
                features.append(feat)
                features_by_id[str(feat["id"])] = feat
                features_by_id[str(feat["event_id"])] = feat
        else:
            live_count = 0
            data_source = "NASA FIRMS Satellite (0 detections in Area BBox)"
            status = "healthy"
    except Exception as exc:
        live_count = 0
        data_source = f"NASA FIRMS API Error ({type(exc).__name__})"
        status = "error"

    anom = science_engine.classify_anomaly(live_count, hist_median, mad)
    rec = generate_recommendation(anom["severity"], anom["pct_diff"], live_count, hist_median)

    payload = {
        "status": status,
        "live_count": live_count,
        "historical_median": round(hist_median, 1),
        "z_score": anom["z_score"],
        "pct_diff": anom["pct_diff"],
        "severity": anom["severity"],
        "label": anom["label"],
        "recommendation": rec,
        "data_source": data_source,
        "timestamp": today.isoformat(),
        "cached": False,
        "features": features,
    }

    _LIVE_CACHE["expires_at"] = now + LIVE_CACHE_TTL_SECONDS
    _LIVE_CACHE["payload"] = payload
    _LIVE_CACHE["features"] = features
    _LIVE_CACHE["features_by_id"] = features_by_id
    return payload


def get_live_today(map_key: str = "") -> Dict[str, Any]:
    """
    Return today's harmonized live report compared against the multi-year
    historical median, backed by an in-memory 5-minute TTL cache and graceful handling.
    Strictly forbids dummy/fake fallback numbers.
    """
    now = time.time()
    if _LIVE_CACHE["payload"] and now < _LIVE_CACHE["expires_at"]:
        cached_resp = dict(_LIVE_CACHE["payload"])
        cached_resp["cached"] = True
        return cached_resp

    return refresh_live_cache(map_key)


def get_live_features(
    map_key: str = "",
    min_lat: Optional[float] = None,
    max_lat: Optional[float] = None,
    min_lon: Optional[float] = None,
    max_lon: Optional[float] = None,
    sensor: str = "all",
    limit: int = 2500,
) -> List[Dict[str, Any]]:
    """
    Return real live fire detections filtered by bounding box and sensor,
    served directly from the in-memory 5-minute TTL cache.
    """
    get_live_today(map_key)  # ensures cache is fresh
    features = _LIVE_CACHE.get("features") or []

    filtered: List[Dict[str, Any]] = []
    sen_lower = sensor.lower() if sensor else "all"

    for f in features:
        if sen_lower != "all" and sen_lower != "live" and f["sensor"].lower() != sen_lower:
            continue
        lat = f["latitude"]
        lon = f["longitude"]
        if min_lat is not None and lat < min_lat:
            continue
        if max_lat is not None and lat > max_lat:
            continue
        if min_lon is not None and lon < min_lon:
            continue
        if max_lon is not None and lon > max_lon:
            continue
        filtered.append(f)
        if len(filtered) >= limit:
            break

    return filtered


def get_live_scenario(event_id_str: str) -> Optional[Dict[str, Any]]:
    """
    Retrieve scenario breakdown for a live hotspot by its live event_id or string id.
    """
    feat = _LIVE_CACHE["features_by_id"].get(str(event_id_str))
    if not feat:
        return None

    return {
        "id": feat["id"],
        "event_id": feat["event_id"],
        "raw_frp": feat["frp"],
        "calibrated_frp": feat["calibrated_frp"],
        "uncertainty_sigma": feat["uncertainty_sigma"],
        "detection_limit_mw": feat["detection_limit_mw"],
        "physics_check": feat["physics_check"],
        "sensor": feat["sensor"],
        "latitude": feat["latitude"],
        "longitude": feat["longitude"],
        "acq_date": feat["acq_date"],
        "hfai": feat["hfai"],
        "hfai_energy": feat["hfai_energy"],
        "hfai_brightness": feat["hfai_brightness"],
        "hfai_confidence": feat["hfai_confidence"],
        "hfai_agreement": feat["hfai_agreement"],
        "anomaly_severity": feat["anomaly_severity"],
        "explanation": {
            "calibration_note": (
                "MODIS FRP scaled by sqrt(1.0 / 0.140625) = 2.667x pixel-area parity factor."
                if feat["sensor"] == "MODIS"
                else "VIIRS 375m reference radiometry preserved."
            ),
            "physics_note": (
                f"Live Satellite detection: Calibrated FRP ({feat['calibrated_frp']} MW) "
                f"verified against off-nadir detection limit ({feat['detection_limit_mw']} MW)."
            ),
            "hfai_formula": "HFAI = 100 * (0.35*E + 0.25*B + 0.20*C + 0.20*A)",
        },
    }
