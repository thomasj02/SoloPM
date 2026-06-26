"""SoloPM HTTP server: a thin FastAPI wrapper over the core service, plus the web app."""

from .app import create_app

__all__ = ["create_app"]
