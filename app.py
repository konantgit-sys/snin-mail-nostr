"""Точка входа: uvicorn app:app --port 8123."""
from mailapp import create_app

app = create_app()
