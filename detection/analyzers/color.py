"""Color & lighting consistency analyzer.

Signals:
  1. Gray-world deviation — natural scenes average near neutral gray; some AI
     models leave a global color cast.
  2. Cb/Cr histogram concentration — over-quantized / overly smooth chroma.
  3. Shadow vs highlight noise ratio — real sensor noise scales with exposure
     (more noise in shadows); AI noise is often constant across luminance.

Needs numpy + Pillow.
"""

from __future__ import annotations

import io
from typing import Any


def analyze(image_bytes: bytes, cfg: dict[str, Any]) -> dict[str, Any]:
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return {"score": None, "signals": {}, "evidence": [], "error": "numpy/Pillow 未安装"}

    ccfg = cfg.get("color", {})
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        rgb = np.asarray(img, dtype=np.float64)
        ycbcr = np.asarray(img.convert("YCbCr"), dtype=np.float64)
    except Exception as exc:  # noqa: BLE001
        return {"score": None, "signals": {}, "evidence": [], "error": str(exc)}

    if rgb.shape[0] < 32 or rgb.shape[1] < 32:
        return {"score": None, "signals": {"too_small": True}, "evidence": [], "error": "图像过小"}

    gray_shift = _gray_world_shift(np, rgb)
    cbcr_conc = _cbcr_concentration(np, ycbcr)
    noise_ratio = _shadow_highlight_noise_ratio(np, ycbcr)

    shift_high = float(ccfg.get("gray_world_shift_high", 0.18))
    conc_high = float(ccfg.get("cbcr_quantization_high", 0.4))
    ratio_low = float(ccfg.get("shadow_highlight_noise_ratio_low", 0.15))

    s_shift = float(np.clip(gray_shift / max(shift_high, 1e-6), 0.0, 1.0))
    s_conc = float(np.clip(cbcr_conc / max(conc_high, 1e-6), 0.0, 1.0))
    # noise_ratio ~= |shadow_noise/highlight_noise - 1|; near 0 (constant noise)
    # is AI-like. Low difference => high AI score.
    s_noise = float(np.clip(1.0 - noise_ratio / max(ratio_low, 1e-6), 0.0, 1.0))
    score = float(np.clip(0.4 * s_shift + 0.3 * s_conc + 0.3 * s_noise, 0.0, 1.0))

    evidence = []
    if s_shift > 0.7:
        evidence.append("整体色彩偏移明显（灰世界假设偏离）")
    if s_noise > 0.7:
        evidence.append("明暗区噪声水平接近恒定（不符合传感器噪声特性）")

    return {
        "score": round(score, 4),
        "signals": {
            "gray_world_shift": round(float(gray_shift), 4),
            "cbcr_concentration": round(float(cbcr_conc), 4),
            "shadow_highlight_noise_ratio": round(float(noise_ratio), 4),
        },
        "evidence": evidence,
        "error": None,
    }


def _gray_world_shift(np, rgb):
    means = rgb.reshape(-1, 3).mean(axis=0) / 255.0
    overall = means.mean() + 1e-9
    return float(np.mean(np.abs(means - overall)) / overall)


def _cbcr_concentration(np, ycbcr):
    # Fraction of chroma energy in the top histogram bins (over-concentration
    # ~ over-quantized / overly smooth chroma).
    conc = []
    for ch in (1, 2):
        hist, _ = np.histogram(ycbcr[:, :, ch], bins=64, range=(0, 255), density=True)
        hist = hist / (hist.sum() + 1e-9)
        top = np.sort(hist)[-5:].sum()  # mass in the 5 tallest bins
        conc.append(top)
    return float(np.mean(conc))


def _shadow_highlight_noise_ratio(np, ycbcr):
    y = ycbcr[:, :, 0]
    shadow_mask = y < np.percentile(y, 25)
    highlight_mask = y > np.percentile(y, 75)
    sn = _local_noise(np, y, shadow_mask)
    hn = _local_noise(np, y, highlight_mask)
    if hn < 1e-6:
        return 0.0
    return float(abs(sn / hn - 1.0))


def _local_noise(np, y, mask):
    # High-frequency residual std within the masked luminance band.
    if mask.sum() < 64:
        return 0.0
    resid = y - _box_blur(np, y, 3)
    vals = resid[mask]
    return float(vals.std())


def _box_blur(np, arr, size):
    from numpy.lib.stride_tricks import sliding_window_view

    pad = size // 2
    padded = np.pad(arr, pad, mode="reflect")
    windows = sliding_window_view(padded, (size, size))
    return windows.mean(axis=(-1, -2))
