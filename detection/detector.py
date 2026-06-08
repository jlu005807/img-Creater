"""Top-level detection orchestration (the module's only public entry point).

Multi-stage cascade:
  1. Capability check — if required deps are missing, return "unavailable".
  2. High-confidence rules (watermark, metadata) — a hit returns immediately.
  3. Multi-signal analysis (frequency, noise, jpeg, color) run only when no
     high-confidence hit; each is independently fault-tolerant.
  4. Scoring engine fuses the analyzer scores into ai / suspicious / real.

Timing is intentionally relaxed (seconds per image are acceptable).
"""

from __future__ import annotations

import time
from typing import Any

from . import capabilities
from .config import load_config
from .scoring import fuse_scores, high_confidence_result, _label


def detector_health() -> dict[str, Any]:
    """Lightweight status for the frontend to show beta availability."""
    return capabilities.capability_report()


def detect_image(image_bytes: bytes, *, filename: str = "") -> dict[str, Any]:
    started = time.monotonic()
    report = capabilities.capability_report()

    if not report["available"]:
        return {
            "available": False,
            "verdict": "unavailable",
            "label": _label("unavailable"),
            "score": None,
            "stages": {},
            "evidence": [],
            "elapsed_ms": _elapsed(started),
            "missing_deps": report["missing_required"],
            "missing_optional": report["missing_optional"],
        }

    if not image_bytes:
        return _error_result("空图片数据", started, report)

    cfg = load_config()
    stages: dict[str, Any] = {}
    evidence: list[str] = []

    # ---- Stage 1: high-confidence rules ----
    for name in ("watermark", "metadata"):
        result = _run_analyzer(name, image_bytes, cfg)
        stages[name] = result
        if result.get("hit"):
            evidence.extend(result.get("evidence", []))
            final = high_confidence_result(1.0, evidence)
            return _assemble(final, stages, evidence, started, report, available=True)

    # ---- Stage 2: multi-signal analysis ----
    module_scores: dict[str, float | None] = {}
    for name in ("frequency", "noise", "jpeg", "color"):
        result = _run_analyzer(name, image_bytes, cfg)
        stages[name] = result
        module_scores[name] = result.get("score")
        evidence.extend(result.get("evidence", []))

    # ---- Stage 3: fusion ----
    fused = fuse_scores(module_scores, cfg["weights"], cfg["thresholds"])
    return _assemble(fused, stages, evidence, started, report, available=True)


# --------------------------------------------------------------------------


def _run_analyzer(name: str, image_bytes: bytes, cfg: dict[str, Any]) -> dict[str, Any]:
    """Import + run one analyzer, isolating all failures to this module."""
    try:
        module = __import__(f"detection.analyzers.{name}", fromlist=["analyze"])
        return module.analyze(image_bytes, cfg)
    except Exception as exc:  # noqa: BLE001 - module-level fault tolerance
        return {"score": None, "signals": {}, "evidence": [], "error": f"{type(exc).__name__}: {exc}"}


def _assemble(core, stages, evidence, started, report, *, available) -> dict[str, Any]:
    out = {
        "available": available,
        "verdict": core["verdict"],
        "label": core["label"],
        "score": core["score"],
        "stages": stages,
        "evidence": evidence,
        "used_weights": core.get("used_weights", {}),
        "elapsed_ms": _elapsed(started),
        "missing_deps": report["missing_required"],
        "missing_optional": report["missing_optional"],
    }
    if "note" in core:
        out["note"] = core["note"]
    return out


def _error_result(message, started, report) -> dict[str, Any]:
    return {
        "available": True,
        "verdict": "suspicious",
        "label": _label("suspicious"),
        "score": None,
        "stages": {},
        "evidence": [],
        "error": message,
        "elapsed_ms": _elapsed(started),
        "missing_deps": report["missing_required"],
        "missing_optional": report["missing_optional"],
    }


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
