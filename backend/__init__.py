"""FIRE HARMONIX backend package — NASA Space Apps Challenge 2026."""
import sys
import types
import json

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

__version__ = "1.0.0"
