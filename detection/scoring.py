"""Scoring engine: fuse per-analyzer anomaly scores into a verdict.

Pure logic, no optional dependencies — fully unit-testable. Each analyzer
contributes a 0..1 anomaly score (higher = more AI-like). Weights come from
config; analyzers that produced no signal (unavailable/errored, score is None)
are dropped and the remaining weights are renormalized, so one missing module
doesn't systematically drag the fused score toward "real".
"""

from __future__ import annotations

from typing import Any


def fuse_scores(
    module_scores: dict[str, float | None],
    weights: dict[str, float],
    thresholds: dict[str, float],
) -> dict[str, Any]:
    """Combine analyzer scores -> {score, verdict, label, used_weights}.

    module_scores: {"frequency": 0.7, "noise": None, ...} — None means the
        analyzer produced no usable signal and is excluded from the fusion.
    """
    contributing = {
        name: score
        for name, score in module_scores.items()
        if score is not None and name in weights and weights[name] > 0
    }

    if not contributing:
        # Nothing to go on — neutral "suspicious" rather than a false "real".
        return {
            "score": 0.5,
            "verdict": "suspicious",
            "label": _label("suspicious"),
            "used_weights": {},
            "note": "no analyzer produced a usable signal",
        }

    total_weight = sum(weights[name] for name in contributing)
    used_weights = {name: weights[name] / total_weight for name in contributing}
    score = sum(used_weights[name] * float(contributing[name]) for name in contributing)
    score = max(0.0, min(1.0, score))

    verdict = _verdict_for(score, thresholds)
    return {
        "score": round(score, 4),
        "verdict": verdict,
        "label": _label(verdict),
        "used_weights": {k: round(v, 4) for k, v in used_weights.items()},
    }


def _verdict_for(score: float, thresholds: dict[str, float]) -> str:
    ai = thresholds.get("ai", 0.6)
    suspicious = thresholds.get("suspicious", 0.3)
    if score >= ai:
        return "ai"
    if score >= suspicious:
        return "suspicious"
    return "real"


def _label(verdict: str) -> str:
    return {
        "ai": "AI 生成",
        "suspicious": "可疑",
        "real": "真实",
        "unavailable": "功能未启用",
    }.get(verdict, verdict)


def high_confidence_result(score: float, evidence: list[str]) -> dict[str, Any]:
    """Shortcut result for a high-confidence rule hit (watermark/metadata)."""
    return {
        "score": round(max(0.0, min(1.0, score)), 4),
        "verdict": "ai",
        "label": _label("ai"),
        "used_weights": {},
        "evidence": list(evidence),
    }
