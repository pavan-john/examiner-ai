"""
Root entry point for Ollama Mock Daemon / Service Adapter.
Delegates to cbse-generator/ollama_mock_service.py
"""

import importlib.util
import os
import sys

_current_dir = os.path.dirname(os.path.abspath(__file__))
_impl_path = os.path.join(_current_dir, "cbse-generator", "ollama_mock_service.py")

if not os.path.exists(_impl_path):
    raise FileNotFoundError(f"Underlying ollama_mock_service implementation not found at {_impl_path}")

_spec = importlib.util.spec_from_file_location("_ollama_mock_service_impl", _impl_path)
if _spec is None or _spec.loader is None:
    raise ImportError(f"Cannot load ollama_mock_service spec from {_impl_path}")

_mod = importlib.util.module_from_spec(_spec)
sys.modules["_ollama_mock_service_impl"] = _mod
_spec.loader.exec_module(_mod)

# Re-export all public symbols to this root module
for _k, _v in _mod.__dict__.items():
    if not _k.startswith("__"):
        globals()[_k] = _v

if __name__ == "__main__":
    _mod.main()
