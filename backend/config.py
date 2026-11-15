import os
from pathlib import Path

try:
    from importlib import import_module
    load_dotenv = import_module("dotenv").load_dotenv
except ModuleNotFoundError:
    def load_dotenv(*args, **kwargs):
        return False

# Base directory resolved using dirname of abspath
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(BACKEND_DIR)

# Load environment variables from .env in BASE_DIR or current directory
env_path = os.path.join(BASE_DIR, ".env")
if os.path.exists(env_path):
    load_dotenv(env_path)
else:
    load_dotenv()

# Support all common environment variable names for NASA FIRMS API key
NASA_MAP_KEY: str = (
    os.getenv("NASA_MAP_KEY")
    or os.getenv("FIRMS_MAP_KEY")
    or os.getenv("NASA_FIRMS_API_KEY")
    or os.getenv("MAP_KEY")
    or ""
).strip()
FIRMS_MAP_KEY: str = NASA_MAP_KEY
NASA_FIRMS_API_KEY: str = NASA_MAP_KEY

# Exact filenames visible inside your Dataset/ folder
FIRES_DATA_PATH: str = os.path.join(BASE_DIR, "Dataset", "final_harmonized_fires (1).csv")
CALENDAR_DATA_PATH: str = os.path.join(BASE_DIR, "Dataset", "365_day_calendar (1).csv")

# Locate real database file regardless of working directory
def find_database_path() -> str:
    candidates = [
        os.path.join(BASE_DIR, "fireharmonix.db"),
        os.path.join(BACKEND_DIR, "fireharmonix.db"),
        os.path.join(os.getcwd(), "fireharmonix.db"),
        os.path.abspath("fireharmonix.db"),
    ]
    for p in candidates:
        if os.path.exists(p) and os.path.getsize(p) > 1024:
            return os.path.abspath(p)
    return os.path.abspath(os.path.join(BASE_DIR, "fireharmonix.db"))

DB_PATH: str = find_database_path()
DATABASE_URL: str = f"sqlite:///{DB_PATH}"

MODIS_PIXEL_KM2: float = 1.0
VIIRS_PIXEL_KM2: float = 0.140625
AREA_RATIO: float = MODIS_PIXEL_KM2 / VIIRS_PIXEL_KM2
DEFAULT_VZA: float = 25.0
FRP_DETECTION_MIN: float = 3.7
FRP_DETECTION_MAX: float = 40.0
INDIA_BBOX: str = os.getenv("FIRMS_BBOX", "68,6,98,38").strip()
FIRMS_DAYS: int = int(os.getenv("FIRMS_DAYS", "2"))
LIVE_CACHE_TTL_SECONDS: int = int(os.getenv("LIVE_CACHE_TTL_SECONDS", "600"))
PORT: int = int(os.getenv("PORT", "8003"))
