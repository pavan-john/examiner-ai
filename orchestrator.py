"""
Root entry point for Chapter Topic Weight Orchestrator.
Delegates to cbse-generator/orchestrator.py with safe module loading.
"""

import importlib.util
import os
import sys

_current_dir = os.path.dirname(os.path.abspath(__file__))
_impl_path = os.path.join(_current_dir, "cbse-generator", "orchestrator.py")

# Preload environment from .env if present
_env_path = os.path.join(_current_dir, ".env")
if os.path.isfile(_env_path):
    try:
        with open(_env_path, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    _k = _k.strip()
                    _v = _v.strip().strip("'\"")
                    if _k and _k not in os.environ:
                        os.environ[_k] = _v
    except Exception:
        pass

if not os.path.exists(_impl_path):
    raise FileNotFoundError(f"Underlying orchestrator implementation not found at {_impl_path}")

# Load implementation module without namespace collision
_spec = importlib.util.spec_from_file_location("_cbse_orchestrator_impl", _impl_path)
if _spec is None or _spec.loader is None:
    raise ImportError(f"Cannot load orchestrator spec from {_impl_path}")

_mod = importlib.util.module_from_spec(_spec)
sys.modules["_cbse_orchestrator_impl"] = _mod
_spec.loader.exec_module(_mod)

# Re-export all public symbols to this root module
for _k, _v in _mod.__dict__.items():
    if not _k.startswith("__"):
        globals()[_k] = _v

if __name__ == "__main__":
    _mod.main()
