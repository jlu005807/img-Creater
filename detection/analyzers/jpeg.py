"""JPEG compression-history analyzer.

Real photos usually carry a compression history (camera JPEG, re-saves);
AI images are often generated once and saved without that history.

Signals:
  - For JPEG input: distance of the embedded quantization table from the
    standard Annex-K table (custom encoders / re-saves drift).
  - Recompression consistency: re-encode at several qualities and look at how
    the error stabilizes. An image already JPEG-compressed shows a
    characteristic error dip near its original quality; a fresh/PNG image
    decays smoothly — low "multi-compression" evidence => more AI-like.

Needs numpy + Pillow.
"""

from __future__ import annotations

import io
from typing import Any

# Standard JPEG luminance quantization table (Annex K).
_STD_LUMA = [
    16, 11, 10, 16, 24, 40, 51, 61,
    12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56,
    14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77,
    24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101,
    72, 92, 95, 98, 112, 100, 103, 99,
]


def analyze(image_bytes: bytes, cfg: dict[str, Any]) -> dict[str, Any]:
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return {"score": None, "signals": {}, "evidence": [], "error": "numpy/Pillow 未安装"}

    jcfg = cfg.get("jpeg", {})
    try:
        img = Image.open(io.BytesIO(image_bytes))
        fmt = (img.format or "").upper()
        rgb = img.convert("RGB")
    except Exception as exc:  # noqa: BLE001
        return {"score": None, "signals": {}, "evidence": [], "error": str(exc)}

    signals: dict[str, Any] = {"format": fmt}
    qtable_score = None
    if fmt in ("JPEG", "JPG"):
        qtable_score = _qtable_distance_score(np, img, jcfg)
        signals["qtable_distance_score"] = None if qtable_score is None else round(qtable_score, 4)

    recompress_evidence = _recompression_consistency(np, Image, rgb, jcfg)
    signals.update(recompress_evidence)

    # Higher score = more AI-like = LESS evidence of a real compression history.
    # multi-compression dip strength in 0..1; invert it.
    dip = float(recompress_evidence.get("recompress_dip_strength", 0.0))
    s_history = float(np.clip(1.0 - dip, 0.0, 1.0))

    parts = [s_history]
    if qtable_score is not None:
        parts.append(qtable_score)
    score = float(np.clip(sum(parts) / len(parts), 0.0, 1.0))

    evidence = []
    if dip > 0.5:
        evidence.append("检测到多次 JPEG 压缩历史（更偏向真实）")

    return {"score": round(score, 4), "signals": signals, "evidence": evidence, "error": None}


def _qtable_distance_score(np, img, jcfg):
    qtables = getattr(img, "quantization", None)
    if not qtables:
        return None
    table = qtables.get(0)
    if not table or len(table) < 64:
        return None
    t = np.array(table[:64], dtype=np.float64)
    std = np.array(_STD_LUMA, dtype=np.float64)
    dist = float(np.mean(np.abs(t - std)) / (np.mean(std) + 1e-9))
    high = float(jcfg.get("qtable_distance_high", 0.4))
    return float(np.clip(dist / max(high, 1e-6), 0.0, 1.0))


def _recompression_consistency(np, Image, rgb, jcfg):
    qualities = list(jcfg.get("recompress_qualities", [70, 80, 90, 95]))
    base = np.asarray(rgb, dtype=np.float64)
    errors = []
    for q in qualities:
        buf = io.BytesIO()
        try:
            rgb.save(buf, format="JPEG", quality=int(q))
            buf.seek(0)
            re = np.asarray(Image.open(buf).convert("RGB"), dtype=np.float64)
        except Exception:  # noqa: BLE001
            continue
        errors.append(float(np.mean((base - re) ** 2)))

    if len(errors) < 3:
        return {"recompress_errors": errors, "recompress_dip_strength": 0.0}

    err = np.array(errors)
    # A previously-JPEG image shows a local minimum (dip) at its original
    # quality rather than monotonic decay. Measure how much the curve deviates
    # from monotonic — that deviation is the "dip strength".
    diffs = np.diff(err)
    monotonic_decreasing = np.all(diffs <= 0)
    if monotonic_decreasing:
        dip = 0.0
    else:
        # Sum of positive jumps (non-monotonic rises) normalized by error scale.
        rises = diffs[diffs > 0].sum()
        dip = float(np.clip(rises / (err.mean() + 1e-9), 0.0, 1.0))

    return {
        "recompress_errors": [round(e, 3) for e in errors],
        "recompress_dip_strength": round(dip, 4),
    }
