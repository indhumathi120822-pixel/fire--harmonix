# FIRE HARMONIX

Physics-calibrated MODIS + VIIRS fire hotspot API for the **NASA Space Apps Challenge 2026** challenge *Harmonization of MODIS and VIIRS Hot Spots*.

FIRE HARMONIX turns a pre-harmonized multi-year archive (and live NASA FIRMS near-real-time pulls) into a single, queryable backend. MODIS 1 km FRP is scaled onto the VIIRS 375 m footprint, each detection is checked against a view-zenith-dependent detection limit, and every point receives a four-term Harmonized Fire Activity Index (HFAI). A day-of-year climatology plus MAD-based modified Z-scores flag whether today is normal, elevated, or unusual relative to history.

The service is a FastAPI application with SQLite. The first process start builds `fireharmonix.db` from your CSVs (about 3.5 million rows / ~180 MB in this project). After that, map queries hit indexes and the live FIRMS path is cached for 15 minutes with a historical fallback if NASA is unreachable. It is intended to run locally or on free Render / Railway web services.

---

## Scientific methodology

### Detection limit (MODIS C6.1 style)

At nadir, Collection 6.1 reliably retrieves fires of about **3.7 MW**. Sensitivity degrades with the square of view zenith angle (projected pixel growth + SNR loss), reaching ~**40 MW** near the scan edge (~65°):

```
DL(VZA) = 3.7 + (40.0 − 3.7) × (VZA / 65)²
```

FIRE HARMONIX evaluates this at a typical mid-swath VZA of **25°**. Detections with raw FRP ≥ DL are marked `PASS`; weaker ones are kept but labelled `SUB_PIXEL_NOISE`.

### Area-parity FRP calibration

- VIIRS 375 m × 375 m pixel area = **0.140625 km²** (reference, scale = 1)
- MODIS 1 km pixel area = **1.0 km²**
- `AREA_RATIO ≈ 7.11`
- `calibrated_frp_MODIS = frp × √(AREA_RATIO)` so energy is compared on a common footprint without over-correcting extensive FRP
- Harmonized (fused) rows keep mean FRP as-is
- 1σ uncertainty: **5%** of calibrated FRP if Harmonized, **12%** otherwise

### HFAI (0–100)

```
HFAI = 100 × (0.35 E + 0.25 B + 0.20 C + 0.20 A)
```

| Term | Meaning | Construction |
|------|---------|--------------|
| **E** energy | Intensity | `log1p(calibrated_frp)` min–max → [0, 1] |
| **B** brightness | Sensor-weighted energy | `calibrated_frp × {Harmonized:1.00, VIIRS:0.85, MODIS:0.70}` min–max |
| **C** confidence | Radiometric trust | `base_conf × exp(−σ / median(σ))`; base 0.95 (Harmonized) / 0.80 (single) |
| **A** agreement | Dual-sensor fuse | 1.0 if Harmonized, else 0.5 |

Live points are scored with the **archive min–max / median-σ scale** stored in SQLite so HFAI is comparable over time.

### MAD-based anomaly detection

Iglewicz–Hoaglin modified Z-score:

```
Z = 0.6745 × (x − median) / max(MAD, 1.0)
pct_diff = 100 × (x − median) / max(median, 1.0)
```

| \|Z\| | Severity | Label |
|------|----------|--------|
| < 1.5 | GREEN | normal |
| 1.5–2.5 | YELLOW | elevated |
| > 2.5 | RED | unusual |

Live briefing compares **today’s hotspot count** to the median of yearly counts for that (month, day). Map points compare **calibrated FRP** to the day-of-year FRP median.

Live MODIS–VIIRS pairs on the same date within **1 km** (haversine) are fused into `sensor="Harmonized"` with mean FRP.

---

## Setup

1. **NASA FIRMS MAP KEY** (32 characters)  
   Request one at [NASA FIRMS MAP KEY](https://firms.modaps.eosdis.nasa.gov/api/map_key/). Copy `.env.example` to `.env` and set:

   ```
   NASA_MAP_KEY=your_32_char_key_here
   ```

2. **Place CSVs** (either location works):

   | Expected name | Also accepted |
   |---------------|----------------|
   | `final_harmonized_fires.csv` | `Dataset/final_harmonized_fires (1).csv` |
   | `365_day_calendar.csv` | `Dataset/365_day_calendar (1).csv` |

   Required fire columns: `latitude, longitude, acq_date, frp, sensor, month, day, year`  
   Required calendar columns: `month, day, avg_fires, avg_frp`

3. **Python 3.10–3.12** for the pinned `requirements.txt` (Render uses `runtime.txt` → 3.11). Python 3.13/3.14 need newer pandas/numpy wheels (`pip install pandas numpy` without those two pins).

4. **Install and run** (see commands below). First start builds `fireharmonix.db` and can take several minutes for ~3.5 million rows. Later starts skip ingest.

---

## API reference

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Redirects to `/docs` |
| GET | `/api/health` | Status, record count, years, date range, live API status |
| GET | `/api/calendar?year=all\|2020\|…` | 365-day climatology + severity |
| GET | `/api/map?year=&sensor=&min_lat=&max_lat=&min_lon=&max_lon=&limit=2500` | Indexed hotspots for the map |
| GET | `/api/scenario/{event_id}` | Physics + HFAI E/B/C/A explanation |
| GET | `/api/live` | FIRMS NRT vs baseline (15 min cache, historical fallback) |
| GET | `/api/export/csv?year=&min_lat=&max_lat=&min_lon=&max_lon=` | Stream CSV |
| GET | `/api/export/sqlite` | Download `fireharmonix.db` |

Interactive docs: `http://127.0.0.1:8003/docs`

---

## Local run

### Windows PowerShell

```powershell
cd "$env:USERPROFILE\OneDrive\Desktop\nasa backend"
python -m pip install -r requirements.txt
python -c "from backend.database import init_db; init_db()"
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8003 --reload
```

Or double-click / run `run_local.bat`.

### Linux / macOS

```bash
chmod +x run_local.sh
./run_local.sh
```

Equivalent:

```bash
python3 -m pip install -r requirements.txt
python3 -c "from backend.database import init_db; init_db()"
python3 -m uvicorn backend.main:app --host 127.0.0.1 --port 8003 --reload
```

---

## Deploy to Render.com

1. Push this folder to a GitHub repository (do **not** commit `.env`; `fireharmonix.db` is optional — it will rebuild from CSVs if present).
2. [Render Dashboard](https://dashboard.render.com) → **New** → **Web Service** → connect the repo.
3. Settings:
   - **Runtime:** Python 3
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `uvicorn backend.main:app --host 0.0.0.0 --port $PORT` (same as `Procfile`)
4. **Environment** → add `NASA_MAP_KEY` = your 32-character FIRMS key.
5. Free instances sleep and have ~512 MB RAM. The first boot that ingests 3.5M rows may need a paid instance **or** commit a pre-built `fireharmonix.db` and the CSVs can stay local. If ingest is skipped because the DB already exists, memory use is much lower.
6. Deploy. Open `https://<service>.onrender.com/docs`.

---

## Deploy to Railway.app

1. Push to GitHub (or use Railway CLI: `railway login` then `railway init`).
2. New project → **Deploy from GitHub** → select the repo.
3. Railway detects the `Procfile` (`web: uvicorn backend.main:app --host 0.0.0.0 --port $PORT`).
4. **Variables** → `NASA_MAP_KEY` = your FIRMS key.
5. Deploy. Open the generated URL + `/docs`.
6. Same memory note as Render: prefer uploading a pre-built SQLite DB for the free/hobby tier, or raise the memory limit for first ingest.

---

## Troubleshooting

| Symptom | What to do |
|---------|------------|
| **NASA API timeout / empty live** | FIRMS calls use a **6 s** timeout. `/api/live` then returns `data_source: historical_fallback` (latest archive year for today’s month/day). Check MAP KEY at the FIRMS portal and that it is 32 hex characters. |
| **CSV schema mismatch** | Fire CSV must include `latitude, longitude, acq_date, frp, sensor`. Calendar must include `month, day`. Colab names with ` (1)` in `Dataset/` are auto-detected. |
| **Port already in use** | Windows: `netstat -ano \| findstr :8000` then `taskkill /PID <pid> /F`. Linux: `lsof -i :8000` then `kill <pid>`. Or pass `--port 8001`. |
| **First start is slow** | Expected: pandas calibration + SQLite insert of millions of rows. Watch logs for `Ingest complete`. Later starts reuse `fireharmonix.db`. |
| **`NASA_MAP_KEY is not set`** | `.env` must be a **file** in the project root, not a folder. `python-dotenv` loads it automatically. |
| **ModuleNotFoundError: backend** | Run uvicorn from the project root (`nasa backend/`), not from inside `backend/`. |

---

## Project structure

```
nasa backend/
├── .env                      # NASA_MAP_KEY (do not commit)
├── .env.example
├── requirements.txt
├── Procfile
├── run_local.sh
├── run_local.bat
├── README.md
├── Dataset/
│   ├── final_harmonized_fires (1).csv
│   └── 365_day_calendar (1).csv
├── fireharmonix.db           # auto-generated on first run
└── backend/
    ├── __init__.py
    ├── config.py
    ├── science_engine.py
    ├── database.py
    ├── live_firms.py
    └── main.py
```

Contact for FIRMS issues: earthdata-support@nasa.gov — FIRMS Team.
