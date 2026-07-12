"""
app.py — top-level entry point for Azure App Service.

Azure's Oryx builder extracts the app to a temp directory and does NOT put the
app root on sys.path, which breaks `import backend`. This shim fixes that by
inserting its own directory onto sys.path before importing the real app.

Locally you can still run either:
    uvicorn app:app --port 8000          (this shim)
    uvicorn backend.app:app --port 8000  (the package directly)
"""
import os
import sys

# Ensure THIS directory (which contains backend/) is importable, wherever
# Azure decided to extract us to.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.app import app  # noqa: E402

__all__ = ["app"]
