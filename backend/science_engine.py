import math
from typing import Dict, Any
import numpy as np
import pandas as pd

# Windows DLL Safe Startup: Wrap tensorflow / keras imports to prevent Windows Application Control blocks
try:
    import tensorflow as tf  # type: ignore
    HAS_TENSORFLOW = True
except Exception:
    tf = None
    HAS_TENSORFLOW = False

from backend.config import (
    AREA_RATIO,
    DEFAULT_VZA,
    FRP_DETECTION_MIN,
    FRP_DETECTION_MAX,
)

# Stefan-Boltzmann constant (W / (m^2 * K^4))
STEFAN_BOLTZMANN_SIGMA: float = 5.670374419e-8


def compute_detection_limit(vza: float = DEFAULT_VZA) -> float:
    """
    Compute theoretical thermal FRP detection limit (MW) as a function of
    satellite View Zenith Angle (VZA) due to off-nadir pixel-area expansion.
    """
    dl = FRP_DETECTION_MIN + (FRP_DETECTION_MAX - FRP_DETECTION_MIN) * ((vza / 65.0) ** 2)
    return round(float(dl), 3)


def apply_physics_calibration(df: pd.DataFrame) -> pd.DataFrame:
    """
    Apply analytical Stefan-Boltzmann physics calibration, cross-sensor
    area-parity FRP scaling (MODIS 1km vs VIIRS 375m: sqrt(1.0 / 0.140625) = 2.667x),
    physics detection-limit verification, and per-observation uncertainty sigma.
    """
    if df.empty:
        for col in ["detection_limit", "physics_check", "calibrated_frp", "uncertainty_sigma"]:
            df[col] = pd.Series(dtype="float64" if col != "physics_check" else "object")
        return df

    out = df.copy()
    dl = compute_detection_limit(DEFAULT_VZA)
    out["detection_limit"] = dl

    sensor_str = out["sensor"].astype(str)
    frp_vals = pd.to_numeric(out["frp"], errors="coerce").fillna(0.0)

    # Cross-sensor area parity factor: MODIS 1km pixel covers 7.11x the surface area of VIIRS 375m
    scale = np.where(sensor_str == "MODIS", math.sqrt(AREA_RATIO), 1.0)
    out["calibrated_frp"] = (frp_vals * scale).round(2)

    # Verify against off-nadir view-angle physical threshold
    out["physics_check"] = np.where(
        out["calibrated_frp"] >= dl, "PASS", "SUB_PIXEL_NOISE"
    )

    # Uncertainty modeling: Harmonized co-observations reduce uncertainty to 5%
    out["uncertainty_sigma"] = np.where(
        sensor_str == "Harmonized",
        out["calibrated_frp"] * 0.05,
        out["calibrated_frp"] * 0.12,
    ).round(3)

    return out


def compute_hfai(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute Harmonized Fire Activity Index (HFAI, 0-100) combining:
      E: Normalized calibrated radiative energy
      B: Sensor-weighted thermal brightness proxy
      C: Uncertainty-attenuated confidence score
      A: Multi-sensor spatial-temporal agreement score
    """
    if df.empty:
        for col in ["hfai", "hfai_energy", "hfai_brightness", "hfai_confidence", "hfai_agreement"]:
            df[col] = pd.Series(dtype="float64")
        return df

    out = df.copy()
    cal_frp = out["calibrated_frp"].clip(lower=0.0)
    log_frp = np.log1p(cal_frp)
    max_log = float(log_frp.quantile(0.99)) if len(log_frp) > 0 else 1.0
    max_log = max(max_log, 1e-6)

    E = np.clip(log_frp / max_log, 0.0, 1.0)

    sensor_str = out["sensor"].astype(str)
    s_weight = np.where(
        sensor_str == "Harmonized", 1.0,
        np.where(sensor_str == "VIIRS", 0.85, 0.70)
    )
    B = np.clip(np.log1p(cal_frp * s_weight) / max_log, 0.0, 1.0)

    base_conf = np.where(sensor_str == "Harmonized", 0.95, 0.80)
    med_sigma = max(float(out["uncertainty_sigma"].median()), 1e-3)
    C = np.clip(base_conf * np.exp(-out["uncertainty_sigma"] / (med_sigma * 5.0)), 0.0, 1.0)

    A = np.where(sensor_str == "Harmonized", 1.0, 0.5)

    out["hfai_energy"] = np.round(E, 4)
    out["hfai_brightness"] = np.round(B, 4)
    out["hfai_confidence"] = np.round(C, 4)
    out["hfai_agreement"] = np.round(A, 4)
    out["hfai"] = np.round(100.0 * (0.35 * E + 0.25 * B + 0.20 * C + 0.20 * A), 2)

    return out


def assign_grid_cell(df: pd.DataFrame) -> pd.DataFrame:
    """
    Assign each observation to a 0.05-degree (~5.5 km) spatial grid cell ID.
    """
    if df.empty:
        df["grid_cell"] = pd.Series(dtype="object")
        return df

    out = df.copy()
    lat_idx = np.floor(out["latitude"] / 0.05).astype(int).astype(str)
    lon_idx = np.floor(out["longitude"] / 0.05).astype(int).astype(str)
    out["grid_cell"] = lat_idx + "_" + lon_idx
    return out


def compute_historical_baseline(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute robust multi-year historical baseline per calendar (month, day)
    using Median and Median Absolute Deviation (MAD).
    """
    cols = ["month", "day", "historical_median_fires", "historical_median_frp", "mad", "std", "record_count"]
    if df.empty:
        return pd.DataFrame(columns=cols)

    yearly_counts = df.groupby(["year", "month", "day"]).size().reset_index(name="daily_count")
    day_stats = yearly_counts.groupby(["month", "day"]).agg(
        historical_median_fires=("daily_count", "median"),
        mad=("daily_count", lambda x: float(np.median(np.abs(x - np.median(x))))),
    ).reset_index()

    frp_stats = df.groupby(["month", "day"]).agg(
        historical_median_frp=("calibrated_frp", "median"),
        std=("calibrated_frp", "std"),
        record_count=("calibrated_frp", "count"),
    ).reset_index()

    baseline = pd.merge(day_stats, frp_stats, on=["month", "day"], how="outer").fillna(0.0)
    baseline["historical_median_fires"] = baseline["historical_median_fires"].round(1)
    baseline["historical_median_frp"] = baseline["historical_median_frp"].round(2)
    baseline["mad"] = baseline["mad"].round(2)
    baseline["std"] = baseline["std"].round(2)
    return baseline[cols]


def classify_anomaly(current_val: float, historical_median: float, mad: float) -> Dict[str, Any]:
    """
    Compute robust Modified Z-Score using MAD and classify fire anomaly severity.
    """
    denom = max(float(mad), max(float(historical_median) * 0.25, 1.0))
    z_score = 0.6745 * (float(current_val) - float(historical_median)) / denom
    pct_diff = ((float(current_val) - float(historical_median)) / max(float(historical_median), 1.0)) * 100.0

    abs_z = abs(z_score)
    if abs_z < 1.5:
        severity, label = "GREEN", "normal"
    elif abs_z <= 2.5:
        severity, label = "YELLOW", "elevated"
    else:
        severity, label = "RED", "unusual"

    return {
        "z_score": round(z_score, 2),
        "pct_diff": round(pct_diff, 1),
        "severity": severity,
        "label": label,
    }


def haversine_km(lat1: np.ndarray, lon1: np.ndarray, lat2: np.ndarray, lon2: np.ndarray) -> np.ndarray:
    """
    Vectorized great-circle Haversine distance in kilometers.
    """
    r = 6371.0088
    phi1, phi2 = np.radians(lat1), np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlambda = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2.0) ** 2 + np.cos(phi1) * np.cos(phi2) * (np.sin(dlambda / 2.0) ** 2)
    return 2.0 * r * np.arctan2(np.sqrt(a), np.sqrt(1.0 - a))


def deduplicate_live(df: pd.DataFrame, distance_km: float = 1.0) -> pd.DataFrame:
    """
    Merge spatially overlapping MODIS and VIIRS detections within `distance_km`
    on the same acquisition date into unified 'Harmonized' events.
    """
    if df.empty or len(df) <= 1:
        return df

    modis = df[df["sensor"] == "MODIS"].reset_index(drop=True)
    viirs = df[df["sensor"] == "VIIRS"].reset_index(drop=True)

    if modis.empty or viirs.empty:
        return df

    matched_viirs = set()
    merged_rows = []

    for _, m_row in modis.iterrows():
        same_day = viirs[viirs["acq_date"] == m_row["acq_date"]]
        if same_day.empty:
            merged_rows.append(m_row.to_dict())
            continue

        dists = haversine_km(
            m_row["latitude"], m_row["longitude"],
            same_day["latitude"].values, same_day["longitude"].values
        )
        close_mask = dists <= distance_km
        if np.any(close_mask):
            close_v = same_day[close_mask]
            matched_viirs.update(close_v.index.tolist())
            merged_rows.append({
                "latitude": round((m_row["latitude"] + close_v["latitude"].mean()) / 2.0, 5),
                "longitude": round((m_row["longitude"] + close_v["longitude"].mean()) / 2.0, 5),
                "acq_date": m_row["acq_date"],
                "frp": round(max(float(m_row["frp"]), float(close_v["frp"].max())), 2),
                "sensor": "Harmonized",
                "month": m_row["month"],
                "day": m_row["day"],
                "year": m_row["year"],
            })
        else:
            merged_rows.append(m_row.to_dict())

    unmatched_viirs = viirs.drop(index=list(matched_viirs), errors="ignore")
    return pd.concat([pd.DataFrame(merged_rows), unmatched_viirs], ignore_index=True)
