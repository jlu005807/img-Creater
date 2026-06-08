"""Noise-texture analyzer.

Real camera noise has spatial correlation and heavy-tailed residual stats;
AI noise tends toward white/uniform or is largely absent.

Signals:
  1. GLCM on the Haar HH sub-band — low correlation / very high uniformity
     suggests AI.
  2. Kurtosis of local variance of the denoise residual — natural sensor
     noise yields heavy tails (high kurtosis); flat AI noise is low.

Needs numpy + Pillow + PyWavelets. The GLCM is computed in pure numpy; scipy
(scipy.signal.convolve2d) is used, when present, only for the residual blur in
the kurtosis path, with a numpy fallback when it is absent.
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
    try:
        import pywt
    except ImportError:
        return {"score": None, "signals": {}, "evidence": [], "error": "PyWavelets 未安装"}

    ncfg = cfg.get("noise", {})
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        arr = np.asarray(img, dtype=np.float64)
    except Exception as exc:  # noqa: BLE001
        return {"score": None, "signals": {}, "evidence": [], "error": str(exc)}

    if arr.shape[0] < 32 or arr.shape[1] < 32:
        return {"score": None, "signals": {"too_small": True}, "evidence": [], "error": "图像过小"}

    # Wavelet HH sub-band.
    try:
        _, (lh, hl, hh) = pywt.dwt2(arr, ncfg.get("wavelet", "haar"))
    except Exception as exc:  # noqa: BLE001
        return {"score": None, "signals": {}, "evidence": [], "error": f"小波分解失败: {exc}"}

    try:
        levels = int(ncfg.get("glcm_levels", 16))
        correlation, uniformity = _glcm_stats(np, hh, levels)
        kurt = _residual_kurtosis(np, arr)

        corr_low = float(ncfg.get("glcm_correlation_low", 0.15))
        kurt_low = float(ncfg.get("residual_kurtosis_low", 1.5))

        # Lower correlation / lower kurtosis => more AI-like.
        s_corr = float(np.clip(1.0 - abs(correlation) / max(corr_low, 1e-6), 0.0, 1.0))
        s_kurt = float(np.clip(1.0 - kurt / max(kurt_low, 1e-6), 0.0, 1.0))
        score = float(np.clip(0.55 * s_corr + 0.45 * s_kurt, 0.0, 1.0))
    except Exception as exc:  # noqa: BLE001 - analyzer must never raise to the caller
        return {"score": None, "signals": {}, "evidence": [], "error": str(exc)}

    evidence = []
    if s_corr > 0.7:
        evidence.append("高频子带空间相关性偏低（噪声偏白噪声/缺失）")

    return {
        "score": round(score, 4),
        "signals": {
            "hh_glcm_correlation": round(float(correlation), 4),
            "hh_glcm_uniformity": round(float(uniformity), 4),
            "residual_var_kurtosis": round(float(kurt), 4),
        },
        "evidence": evidence,
        "error": None,
    }


def _glcm_stats(np, sub, levels):
    # Quantize the sub-band to `levels` and build a horizontal-neighbour GLCM.
    s = sub.copy()
    lo, hi = np.percentile(s, 1), np.percentile(s, 99)
    if hi <= lo:
        return 0.0, 1.0
    q = np.clip(((s - lo) / (hi - lo) * (levels - 1)), 0, levels - 1).astype(np.int32)
    a = q[:, :-1].ravel()
    b = q[:, 1:].ravel()
    glcm = np.zeros((levels, levels), dtype=np.float64)
    np.add.at(glcm, (a, b), 1.0)
    total = glcm.sum()
    if total == 0:
        return 0.0, 1.0
    glcm /= total

    idx = np.arange(levels)
    mi = (idx[:, None] * glcm).sum()
    mj = (idx[None, :] * glcm).sum()
    si = np.sqrt(((idx[:, None] - mi) ** 2 * glcm).sum())
    sj = np.sqrt(((idx[None, :] - mj) ** 2 * glcm).sum())
    if si < 1e-9 or sj < 1e-9:
        correlation = 0.0
    else:
        cov = ((idx[:, None] - mi) * (idx[None, :] - mj) * glcm).sum()
        correlation = float(cov / (si * sj))
    uniformity = float((glcm ** 2).sum())  # angular second moment / energy
    return correlation, uniformity


def _residual_kurtosis(np, arr):
    # Denoise residual via a 3x3 mean blur; kurtosis of local (8x8) variances.
    k = np.ones((3, 3)) / 9.0
    blurred = _convolve2d_same(np, arr, k)
    resid = arr - blurred
    bs = 8
    h, w = resid.shape
    variances = []
    for y in range(0, h - bs + 1, bs):
        for x in range(0, w - bs + 1, bs):
            variances.append(resid[y:y + bs, x:x + bs].var())
    v = np.array(variances)
    if v.size < 8:
        # Under-determined: return Gaussian-equivalent kurtosis (~3) so the
        # score maps to neutral rather than maximally AI-like.
        return 3.0
    v = v - v.mean()
    s = v.std() + 1e-9
    return float(np.mean((v / s) ** 4))  # raw kurtosis (Gaussian ~3)


def _convolve2d_same(np, arr, kernel):
    try:
        from scipy.signal import convolve2d

        return convolve2d(arr, kernel, mode="same", boundary="symm")
    except Exception:  # noqa: BLE001 - manual fallback via padded FFT-free blur
        from numpy.lib.stride_tricks import sliding_window_view

        pad = kernel.shape[0] // 2
        padded = np.pad(arr, pad, mode="reflect")
        windows = sliding_window_view(padded, kernel.shape)
        return np.einsum("ijkl,kl->ij", windows, kernel)
