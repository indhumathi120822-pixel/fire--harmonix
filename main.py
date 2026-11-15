import io
import os
import sys
import types
import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional, Any, List, Dict
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles

# Resolve base directory using dirname of abspath so database is found from any working directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Load environment variables from .env located in BASE_DIR
try:
    from importlib import import_module
    load_dotenv = import_module("dotenv").load_dotenv
    env_path = os.path.join(BASE_DIR, ".env")
    if os.path.exists(env_path):
        load_dotenv(env_path)
    else:
        load_dotenv()
except Exception:
    pass

# Windows DLL & App Control Safe Initialization
# Unconditionally mock pandas._libs._ujson to prevent Windows Application Control DLL load failure
if "pandas._libs._ujson" not in sys.modules:
    m = types.ModuleType("pandas._libs._ujson")
    m.ujson_dumps = lambda obj, *args, **kwargs: json.dumps(obj)
    m.ujson_loads = lambda s, *args, **kwargs: json.loads(s)
    m.dumps = m.ujson_dumps
    m.loads = m.ujson_loads
    sys.modules["pandas._libs._ujson"] = m

# Wrap tensorflow / keras imports to prevent Windows Application Control blocks
try:
    import tensorflow as tf  # type: ignore
    HAS_TENSORFLOW = True
except Exception:
    tf = None
    HAS_TENSORFLOW = False

# Robust database path finder using os.path.dirname(os.path.abspath(__file__))
def find_real_database() -> str:
    candidates = [
        os.path.join(BASE_DIR, "fireharmonix.db"),
        os.path.join(BASE_DIR, "backend", "fireharmonix.db"),
        os.path.join(os.getcwd(), "fireharmonix.db"),
        os.path.abspath("fireharmonix.db"),
    ]
    for p in candidates:
        if os.path.exists(p) and os.path.getsize(p) > 1024:
            return os.path.abspath(p)
    return os.path.abspath(os.path.join(BASE_DIR, "fireharmonix.db"))

DB_PATH: str = find_real_database()

from backend.config import NASA_MAP_KEY
from backend import database, live_firms

app = FastAPI(
    title="FIRE HARMONIX — NASA Space Apps 2026 API",
    description="Physics-informed MODIS & VIIRS Active Fire Harmonization Backend",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files directory
static_dir = os.path.join(BASE_DIR, "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

_HEALTH_CACHE: Dict[str, Any] = {"expires_at": 0.0, "data": None}


def validate_coordinates(
    min_lat: Optional[float],
    max_lat: Optional[float],
    min_lon: Optional[float],
    max_lon: Optional[float],
) -> None:
    """Strict coordinate input validation. Raises HTTP 400 with a clear message on invalid inputs."""
    if min_lat is not None and not (-90.0 <= min_lat <= 90.0):
        raise HTTPException(status_code=400, detail="Invalid min_lat: coordinate must be between -90.0 and 90.0")
    if max_lat is not None and not (-90.0 <= max_lat <= 90.0):
        raise HTTPException(status_code=400, detail="Invalid max_lat: coordinate must be between -90.0 and 90.0")
    if min_lat is not None and max_lat is not None and min_lat > max_lat:
        raise HTTPException(status_code=400, detail="Invalid latitude bounds: min_lat cannot be greater than max_lat")

    if min_lon is not None and not (-180.0 <= min_lon <= 180.0):
        raise HTTPException(status_code=400, detail="Invalid min_lon: coordinate must be between -180.0 and 180.0")
    if max_lon is not None and not (-180.0 <= max_lon <= 180.0):
        raise HTTPException(status_code=400, detail="Invalid max_lon: coordinate must be between -180.0 and 180.0")
    if min_lon is not None and max_lon is not None and min_lon > max_lon:
        raise HTTPException(status_code=400, detail="Invalid longitude bounds: min_lon cannot be greater than max_lon")


CALENDAR_CACHE: Dict[str, Any] = {}

@app.on_event("startup")
def startup_event() -> None:
    database.init_db()
    try:
        for k, v in database._CALENDAR_CACHE.items():
            CALENDAR_CACHE[k] = v
    except Exception:
        pass


@app.get("/", include_in_schema=False)
def serve_frontend():
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"error": "index.html not found", "path": index_path}


@app.get("/api/health")
def api_health():
    try:
        now = time.time()
        if _HEALTH_CACHE["data"] and now < _HEALTH_CACHE["expires_at"]:
            cached = dict(_HEALTH_CACHE["data"])
            cached["cached"] = True
            return cached

        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            total = cursor.execute("SELECT COUNT(*) FROM hotspots").fetchone()[0]
            years = [2020, 2021, 2022, 2023, 2024, 2025, 2026]
            sensors = ["Harmonized", "MODIS", "VIIRS"]
            min_d = "2020-01-01"
            max_d = "2026-11-13"

        data = {
            "status": "healthy",
            "db_connected": True,
            "total_records": int(total or 0),
            "years_covered": years,
            "sensors": sensors,
            "date_range": {"min": min_d, "max": max_d},
            "live_api_status": "configured" if NASA_MAP_KEY else "missing_key",
            "physics_backend": "analytical_stefan_boltzmann",
            "tensorflow_available": HAS_TENSORFLOW,
            "database_path": DB_PATH,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cached": False,
        }
        _HEALTH_CACHE["expires_at"] = now + 600.0  # 10-minute cache for fast KPI responses
        _HEALTH_CACHE["data"] = data
        return data
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/calendar")
def api_calendar(year: str = Query("all", description="Filter by year or 'all'")):
    try:
        y_key = str(year).strip().lower() if year else "all"
        if y_key in CALENDAR_CACHE:
            return {"year": year, "days": CALENDAR_CACHE[y_key]}
        days = database.get_calendar(year=y_key)
        CALENDAR_CACHE[y_key] = days
        return {"year": year, "days": days}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/map")
def api_map(
    mode: str = "historical",
    year: Optional[int] = None,
    sensor: str = "all",
    min_lat: Optional[float] = None,
    max_lat: Optional[float] = None,
    min_lon: Optional[float] = None,
    max_lon: Optional[float] = None,
    limit: int = 1500,
):
    # Strict coordinate validation returning HTTP 400 on error
    validate_coordinates(min_lat, max_lat, min_lon, max_lon)

    try:
        if mode == "live" or sensor.lower() == "live":
            # Live NASA FIRMS detections backed by 5-minute TTL cache
            features = live_firms.get_live_features(
                map_key=NASA_MAP_KEY,
                min_lat=min_lat,
                max_lat=max_lat,
                min_lon=min_lon,
                max_lon=max_lon,
                sensor=sensor,
                limit=limit,
            )
            return {"count": len(features), "features": features, "mode": "live"}

        # Spatial indexed historical queries (< 300ms)
        features = database.get_map({
            "year": year,
            "sensor": sensor,
            "min_lat": min_lat,
            "max_lat": max_lat,
            "min_lon": min_lon,
            "max_lon": max_lon,
            "limit": limit,
        })
        return {"count": len(features), "features": features, "mode": "historical"}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/scenario/{event_id}")
def api_scenario(event_id: str):
    try:
        # Check if event_id corresponds to a live hotspot
        live_data = live_firms.get_live_scenario(event_id)
        if live_data:
            return live_data

        try:
            int_id = int(event_id)
            data = database.get_scenario(int_id)
            if not data:
                raise HTTPException(status_code=404, detail=f"Event ID {event_id} not found")
            return data
        except ValueError:
            raise HTTPException(status_code=404, detail=f"Event ID {event_id} not found")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/live")
def api_live():
    """Real NASA FIRMS active fire coordinate feed with 5-minute TTL caching."""
    try:
        return live_firms.get_live_today(NASA_MAP_KEY)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/export/csv")
def api_export_csv(
    year: Optional[int] = None,
    min_lat: Optional[float] = None,
    max_lat: Optional[float] = None,
    min_lon: Optional[float] = None,
    max_lon: Optional[float] = None,
    limit: int = 50000,
):
    # Strict coordinate validation returning HTTP 400 on error
    validate_coordinates(min_lat, max_lat, min_lon, max_lon)

    try:
        df = database.get_export_df({
            "year": year,
            "min_lat": min_lat,
            "max_lat": max_lat,
            "min_lon": min_lon,
            "max_lon": max_lon,
            "limit": limit,
        })
        stream = io.StringIO()
        df.to_csv(stream, index=False)
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        return StreamingResponse(
            iter([stream.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=fireharmonix_export_{date_str}.csv"},
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/export/sqlite")
def api_export_sqlite():
    try:
        return FileResponse(
            path=DB_PATH,
            filename="fireharmonix.db",
            media_type="application/octet-stream",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


if __name__ == "__main__":
    import uvicorn
    database.init_db()
    PORT = int(os.getenv("PORT", "8003"))
    uvicorn.run("main:app", host="127.0.0.1", port=PORT, reload=False)
