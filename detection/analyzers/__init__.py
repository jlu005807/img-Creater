"""Analyzer sub-modules.

Each analyzer exposes ``analyze(image_bytes, cfg) -> dict`` returning at least:
  {"score": float|None, "signals": {...}, "evidence": [str], "error": str|None}

``score`` is a 0..1 anomaly score (higher = more AI-like), or None when the
analyzer could not run (missing dependency / decode error) — None is excluded
from fusion by the scoring engine. Analyzers must never raise to the caller;
they catch their own errors and report via the ``error`` field.
"""
