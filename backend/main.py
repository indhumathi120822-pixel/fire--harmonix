import sys
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from main import (
    app,
    api_health,
    api_map,
    api_calendar,
    api_scenario,
    api_live,
    api_export_csv,
    api_export_sqlite,
    serve_frontend,
    validate_coordinates,
)

if __name__ == "__main__":
    import uvicorn
    from backend import database
    database.init_db()
    PORT = int(os.getenv("PORT", "8003"))
    uvicorn.run("backend.main:app", host="127.0.0.1", port=PORT, reload=False)
