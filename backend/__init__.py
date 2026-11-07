"""Dashboard backend: runs the day loop and serves the API the frontend expects."""
from .app import create_app
from .service import Backend, GoalNotFound

__all__ = ["Backend", "GoalNotFound", "create_app"]
