"""Shared test package for the gpt-img2-creater project.

Importing this package sets ``LOG_LEVEL=WARNING`` so that structured JSON
logging configured by ``create_app()`` does not flood test output with
noise.  Tests that explicitly assert on log output override the level in
their own setUp/tearDown.
"""
import os

# Set before any test imports backend.app, which calls
# configure_structured_logging() at import time.
os.environ.setdefault("LOG_LEVEL", "WARNING")
