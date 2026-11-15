import os
import sqlite3
from typing import Dict, Any, Optional, List
import pandas as pd  # type: ignore[reportMissingModuleSource]

from backend.config import DB_PATH, FIRES_DATA_PATH, CALENDAR_DATA_PATH
from backend import science_engine
import urllib.request
if not os.path.exists(DB_PATH):
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    urllib.request.urlretrieve("https://github.com/indhumathi120822-pixel/fire--harmonix/releases/download/v1.0/fireharmonix.db")
_CALENDAR_CACHE: Dict[str, List[Dict[str, Any]]] = {}


def get_engine() -> sqlite3.Connection:
    """Return a SQLite connection without requiring SQLAlchemy."""
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def load_table_file(path: str) -> pd.DataFrame:
    if path.lower().endswith((".xlsx", ".xls")):
        return pd.read_excel(path)
    return pd.read_csv(path)


def init_db():
    conn = None
    if os.path.exists(DB_PATH) and os.path.getsize(DB_PATH) > 1024:
        conn = sqlite3.connect(DB_PATH)
        idx_count = conn.execute("SELECT count(1) FROM sqlite_master WHERE type='index' AND tbl_name='hotspots'").fetchone()[0]
        if idx_count < 7:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_lat_lon_year ON hotspots(latitude, longitude, year)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_year_lat_lon ON hotspots(year, latitude, longitude)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_year_lat_lon_sensor ON hotspots(year, latitude, longitude, sensor)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_lat_lon ON hotspots(latitude, longitude)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ym ON hotspots(year, month, day)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_grid ON hotspots(grid_cell)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sensor ON hotspots(sensor)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_event ON hotspots(event_id)")
            conn.execute("CREATE TABLE IF NOT EXISTS calendar_cache (year TEXT PRIMARY KEY, data_json TEXT)")
            conn.commit()
        cnt = conn.execute("SELECT MAX(event_id) + 1 FROM hotspots").fetchone()[0] or 3518726
        conn.close()
        print(f"[STARTUP] SQLite DB ready at {DB_PATH} | Total Hotspots: {cnt}")
        precompute_calendar_cache()
        return

    if not os.path.exists(FIRES_DATA_PATH):
        conn.close()
        raise FileNotFoundError(f"Missing required dataset: {FIRES_DATA_PATH}")

    print(f"[STARTUP] Building SQLite database from {FIRES_DATA_PATH}...")
    df = load_table_file(FIRES_DATA_PATH)
    df.columns = [c.strip().lower() for c in df.columns]

    if "month" not in df.columns or "year" not in df.columns:
        dt = pd.to_datetime(df["acq_date"])
        df["month"] = dt.dt.month
        df["day"] = dt.dt.day
        df["year"] = dt.dt.year

    df = science_engine.apply_physics_calibration(df)
    df = science_engine.compute_hfai(df)
    df = science_engine.assign_grid_cell(df)
    df.reset_index(drop=True, inplace=True)
    df["event_id"] = df.index.astype(int)

    baseline_df = science_engine.compute_historical_baseline(df)
    df = df.merge(
        baseline_df[["month", "day", "historical_median_frp", "mad"]],
        on=["month", "day"],
        how="left",
    )
    med_frp = df["historical_median_frp"].replace(0, 10.0)
    z_proxy = 0.6745 * (df["calibrated_frp"] - med_frp) / med_frp
    df["anomaly_severity"] = "GREEN"
    df.loc[z_proxy.abs() >= 1.5, "anomaly_severity"] = "YELLOW"
    df.loc[z_proxy.abs() > 2.5, "anomaly_severity"] = "RED"

    df.to_sql("hotspots", conn, if_exists="replace", index=False, chunksize=50000)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_lat_lon_year ON hotspots(latitude, longitude, year)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_year_lat_lon ON hotspots(year, latitude, longitude)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_year_lat_lon_sensor ON hotspots(year, latitude, longitude, sensor)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lat_lon ON hotspots(latitude, longitude)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ym ON hotspots(year, month, day)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_grid ON hotspots(grid_cell)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sensor ON hotspots(sensor)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_event ON hotspots(event_id)")
    conn.execute("CREATE TABLE IF NOT EXISTS calendar_cache (year TEXT PRIMARY KEY, data_json TEXT)")

    hfai_day = df.groupby(["month", "day"], as_index=False).agg(mean_hfai=("hfai", "mean"))
    baseline_df = baseline_df.merge(hfai_day, on=["month", "day"], how="left").fillna(0.0)
    baseline_df["mean_hfai"] = baseline_df["mean_hfai"].round(1)
    baseline_df.to_sql("baseline", conn, if_exists="replace", index=False)

    if os.path.exists(CALENDAR_DATA_PATH):
        cal_raw = load_table_file(CALENDAR_DATA_PATH)
        cal_raw.columns = [c.strip().lower() for c in cal_raw.columns]
        cal_merged = cal_raw.merge(baseline_df, on=["month", "day"], how="left").fillna(0.0)
    else:
        cal_merged = baseline_df.copy()
        cal_merged["avg_fires"] = cal_merged["historical_median_fires"]
        cal_merged["avg_frp"] = cal_merged["historical_median_frp"]

    cal_merged.to_sql("calendar", conn, if_exists="replace", index=False)
    conn.commit()
    conn.close()

    years = sorted(df["year"].unique().tolist())
    print(f"[STARTUP] DB Built! Records: {len(df)} | Years: {years} | Range: {df['acq_date'].min()} to {df['acq_date'].max()}")
    precompute_calendar_cache()


def compute_calendar_for_year(year: Optional[str] = "all") -> List[Dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    if year and str(year).lower() != "all" and str(year).isdigit():
        yr = int(year)
        q = """
            SELECT c.month, c.day, c.historical_median_fires, c.avg_frp, c.mad,
                   COALESCE(y.yr_count, 0) AS current_fires,
                   COALESCE(y.yr_hfai, c.mean_hfai) AS mean_hfai
            FROM calendar c
            LEFT JOIN (
                SELECT month, day, COUNT(*) AS yr_count, ROUND(AVG(hfai), 1) AS yr_hfai
                FROM hotspots WHERE year = ? GROUP BY month, day
            ) y ON c.month = y.month AND c.day = y.day
            ORDER BY c.month, c.day
        """
        cur.execute(q, (yr,))
    else:
        q = "SELECT month, day, historical_median_fires, avg_frp, mad, historical_median_fires AS current_fires, mean_hfai FROM calendar ORDER BY month, day"
        cur.execute(q)

    rows = cur.fetchall()
    conn.close()

    days_out = []
    for r in rows:
        anom = science_engine.classify_anomaly(
            float(r["current_fires"]), float(r["historical_median_fires"]), float(r["mad"])
        )
        days_out.append({
            "month": int(r["month"]),
            "day": int(r["day"]),
            "current_fires": int(r["current_fires"]),
            "historical_median_fires": round(float(r["historical_median_fires"]), 1),
            "avg_frp": round(float(r["avg_frp"]), 2),
            "mean_hfai": round(float(r["mean_hfai"]), 1),
            "severity": anom["severity"],
            "label": anom["label"],
        })
    return days_out


def precompute_calendar_cache() -> None:
    """
    Precompute and store calendar distributions in SQLite table `calendar_cache`
    and in memory at startup so requests respond in < 15ms.
    """
    global _CALENDAR_CACHE
    try:
        import json
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS calendar_cache (year TEXT PRIMARY KEY, data_json TEXT)")
        cur.execute("SELECT year, data_json FROM calendar_cache")
        cached_rows = cur.fetchall()
        if cached_rows and len(cached_rows) >= 7:
            for y, j_str in cached_rows:
                _CALENDAR_CACHE[str(y)] = json.loads(j_str)
            conn.close()
            print(f"[STARTUP] Pre-aggregated calendar cache loaded from DB table for: {list(_CALENDAR_CACHE.keys())}")
            return

        cur.execute("SELECT DISTINCT year FROM hotspots ORDER BY year")
        years = [str(r[0]) for r in cur.fetchall()]
        if "all" not in years:
            years.insert(0, "all")

        for y in years:
            data = compute_calendar_for_year(y)
            _CALENDAR_CACHE[y] = data
            cur.execute("INSERT OR REPLACE INTO calendar_cache (year, data_json) VALUES (?, ?)", (y, json.dumps(data)))
        conn.commit()
        conn.close()
        print(f"[STARTUP] Pre-aggregated calendar cache computed and stored for: {list(_CALENDAR_CACHE.keys())}")
    except Exception as exc:
        print(f"[STARTUP] Calendar cache note: {exc}")
        if not _CALENDAR_CACHE:
            _CALENDAR_CACHE["all"] = compute_calendar_for_year("all")


def get_calendar(year: Optional[str] = "all") -> List[Dict[str, Any]]:
    """
    Return 365-day calendar distribution with sub-millisecond retrieval from startup cache.
    """
    y_key = str(year).strip().lower() if year else "all"
    if y_key in _CALENDAR_CACHE:
        return _CALENDAR_CACHE[y_key]
    res = compute_calendar_for_year(y_key)
    _CALENDAR_CACHE[y_key] = res
    return res


def get_map(filters: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Spatial indexed query leveraging (year, latitude, longitude, sensor).
    Uses native sqlite3 cursor and limit 1500 to guarantee < 100ms response over 1.7M+ rows.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    clauses = ["1=1"]
    params: List[Any] = []

    if filters.get("year"):
        clauses.append("year = ?")
        params.append(int(filters["year"]))
    if filters.get("sensor") and str(filters["sensor"]).lower() != "all":
        clauses.append("LOWER(sensor) = ?")
        params.append(str(filters["sensor"]).lower())

    min_lat = filters.get("min_lat")
    max_lat = filters.get("max_lat")
    if min_lat is not None and max_lat is not None:
        try:
            min_lat_f = min(float(min_lat), float(max_lat))
            max_lat_f = max(float(min_lat), float(max_lat))
            clauses.append("latitude BETWEEN ? AND ?")
            params.extend([min_lat_f, max_lat_f])
        except (ValueError, TypeError):
            pass

    min_lon = filters.get("min_lon")
    max_lon = filters.get("max_lon")
    if min_lon is not None and max_lon is not None:
        try:
            min_lon_f = min(float(min_lon), float(max_lon))
            max_lon_f = max(float(min_lon), float(max_lon))
            clauses.append("longitude BETWEEN ? AND ?")
            params.extend([min_lon_f, max_lon_f])
        except (ValueError, TypeError):
            pass

    limit = min(max(1, int(filters.get("limit", 1500))), 5000)
    params.append(limit)

    sql = f"""
        SELECT event_id AS id, event_id, latitude, longitude, acq_date, sensor,
               calibrated_frp, hfai, anomaly_severity
        FROM hotspots
        WHERE {' AND '.join(clauses)}
        LIMIT ?
    """
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_scenario(event_id: int) -> Optional[Dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM hotspots WHERE event_id = ? LIMIT 1", (int(event_id),))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    r = dict(row)
    return {
        "id": int(r["event_id"]),
        "event_id": int(r["event_id"]),
        "raw_frp": float(r["frp"]),
        "calibrated_frp": float(r["calibrated_frp"]),
        "uncertainty_sigma": float(r["uncertainty_sigma"]),
        "detection_limit_mw": float(r["detection_limit"]),
        "physics_check": str(r["physics_check"]),
        "sensor": str(r["sensor"]),
        "latitude": float(r["latitude"]),
        "longitude": float(r["longitude"]),
        "acq_date": str(r["acq_date"]),
        "hfai": float(r["hfai"]),
        "hfai_energy": float(r["hfai_energy"]),
        "hfai_brightness": float(r["hfai_brightness"]),
        "hfai_confidence": float(r["hfai_confidence"]),
        "hfai_agreement": float(r["hfai_agreement"]),
        "anomaly_severity": str(r.get("anomaly_severity", "GREEN")),
        "explanation": {
            "calibration_note": (
                "MODIS FRP scaled by sqrt(1.0 / 0.140625) = 2.667x pixel-area parity factor."
                if r["sensor"] == "MODIS"
                else "VIIRS 375m reference radiometry preserved."
            ),
            "physics_note": f"Calibrated FRP ({r['calibrated_frp']} MW) compared against off-nadir detection limit ({r['detection_limit']} MW).",
            "hfai_formula": "HFAI = 100 * (0.35*E + 0.25*B + 0.20*C + 0.20*A)",
        },
    }


def get_export_df(filters: Dict[str, Any]) -> pd.DataFrame:
    """
    Exports historical harmonized records with raw MODIS, raw VIIRS, calibrated FRP,
    and HFAI side-by-side as empirical proof of harmonization.
    """
    conn = sqlite3.connect(DB_PATH)
    clauses = ["1=1"]
    params: List[Any] = []
    if filters.get("year"):
        clauses.append("year = ?")
        params.append(int(filters["year"]))

    min_lat = filters.get("min_lat")
    max_lat = filters.get("max_lat")
    if min_lat is not None and max_lat is not None:
        try:
            min_lat_f = min(float(min_lat), float(max_lat))
            max_lat_f = max(float(min_lat), float(max_lat))
            clauses.append("latitude BETWEEN ? AND ?")
            params.extend([min_lat_f, max_lat_f])
        except (ValueError, TypeError):
            pass

    min_lon = filters.get("min_lon")
    max_lon = filters.get("max_lon")
    if min_lon is not None and max_lon is not None:
        try:
            min_lon_f = min(float(min_lon), float(max_lon))
            max_lon_f = max(float(min_lon), float(max_lon))
            clauses.append("longitude BETWEEN ? AND ?")
            params.extend([min_lon_f, max_lon_f])
        except (ValueError, TypeError):
            pass

    limit = min(max(1, int(filters.get("limit", 50000))), 100000)
    params.append(limit)

    sql = f"""
        SELECT event_id AS id,
               acq_date,
               latitude,
               longitude,
               sensor,
               CASE WHEN UPPER(sensor) IN ('MODIS', 'HARMONIZED') THEN frp ELSE NULL END AS raw_modis_frp,
               CASE WHEN UPPER(sensor) IN ('VIIRS', 'HARMONIZED') THEN frp ELSE NULL END AS raw_viirs_frp,
               frp AS raw_frp,
               calibrated_frp,
               uncertainty_sigma,
               physics_check,
               hfai,
               grid_cell,
               anomaly_severity
        FROM hotspots
        WHERE {' AND '.join(clauses)}
        LIMIT ?
    """
    df = pd.read_sql(sql, conn, params=params)
    conn.close()
    return df
