"""Frequency-domain analyzer.

Three complementary signals on the grayscale image:
  1. Radial power spectrum deviation from a natural ~power-law falloff
     (averaged over overlapping blocks).
  2. Periodic spectral peak density — GAN/diffusion upsamplers leave regular
     grid spikes; natural images don't.
  3. Global DCT coefficient histogram chi-square vs a Laplacian model.

Needs numpy + Pillow. scipy.fftpack provides the DCT signal; if scipy is
absent that one signal is skipped (contributes 0) while the radial-power and
peak-density signals still run.
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

    fcfg = cfg.get("frequency", {})
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        arr = np.asarray(img, dtype=np.float64)
    except Exception as exc:  # noqa: BLE001
        return {"score": None, "signals": {}, "evidence": [], "error": str(exc)}

    if arr.shape[0] < 64 or arr.shape[1] < 64:
        return {"score": None, "signals": {"too_small": True}, "evidence": [], "error": "图像过小"}

    radial_dev, peak_density = _block_spectral_stats(np, arr, fcfg)
    dct_chi2 = _dct_chi2(np, arr)

    # Normalize each signal to 0..1 against config "high" thresholds.
    s_radial = _ratio(radial_dev, fcfg.get("radial_deviation_high", 0.35))
    s_peak = _ratio(peak_density, fcfg.get("peak_density_high", 0.02))
    s_dct = _ratio(dct_chi2, fcfg.get("dct_chi2_high", 0.5))
    score = float(np.clip(0.4 * s_radial + 0.35 * s_peak + 0.25 * s_dct, 0.0, 1.0))

    evidence = []
    if s_peak > 0.7:
        evidence.append("频谱存在规律性尖峰（上采样栅格痕迹）")
    if s_radial > 0.7:
        evidence.append("径向频谱偏离自然图像统计")

    return {
        "score": round(score, 4),
        "signals": {
            "radial_deviation": round(float(radial_dev), 4),
            "peak_density": round(float(peak_density), 4),
            "dct_chi2": round(float(dct_chi2), 4),
        },
        "evidence": evidence,
        "error": None,
    }


def _block_spectral_stats(np, arr, fcfg):
    block = int(fcfg.get("block_size", 256))
    overlap = float(fcfg.get("block_overlap", 0.5))
    max_blocks = int(fcfg.get("max_blocks", 12))
    peak_ratio = float(fcfg.get("peak_ratio", 4.0))
    h, w = arr.shape
    block = min(block, h, w)
    step = max(1, int(block * (1 - overlap)))

    radial_devs = []
    peak_densities = []
    count = 0
    for y in range(0, h - block + 1, step):
        for x in range(0, w - block + 1, step):
            if count >= max_blocks:
                break
            patch = arr[y:y + block, x:x + block]
            patch = patch - patch.mean()
            window = np.outer(np.hanning(block), np.hanning(block))
            spec = np.abs(np.fft.fftshift(np.fft.fft2(patch * window)))
            spec_log = np.log1p(spec)

            radial = _radial_profile(np, spec_log)
            radial_devs.append(_powerlaw_deviation(np, radial))
            peak_densities.append(_peak_density(np, spec, peak_ratio))
            count += 1
        if count >= max_blocks:
            break

    if not radial_devs:
        return 0.0, 0.0
    return float(np.mean(radial_devs)), float(np.mean(peak_densities))


def _radial_profile(np, spec):
    cy, cx = np.array(spec.shape) // 2
    y, x = np.indices(spec.shape)
    r = np.sqrt((x - cx) ** 2 + (y - cy) ** 2).astype(int)
    nbins = int(r.max()) + 1
    tbin = np.bincount(r.ravel(), spec.ravel(), minlength=nbins)
    nr = np.bincount(r.ravel(), minlength=nbins)
    nr[nr == 0] = 1
    return tbin / nr


def _powerlaw_deviation(np, radial):
    # Natural images: radial power falls ~linearly in log-log. Fit a line to
    # the mid-band and measure normalized residual.
    n = len(radial)
    if n < 16:
        return 0.0
    lo, hi = max(1, n // 16), n - 1
    freqs = np.arange(lo, hi)
    vals = radial[lo:hi]
    mask = vals > 0
    if mask.sum() < 8:
        return 0.0
    lf = np.log(freqs[mask] + 1e-9)
    lv = np.log(vals[mask] + 1e-9)
    a, b = np.polyfit(lf, lv, 1)
    resid = lv - (a * lf + b)
    return float(np.std(resid) / (np.std(lv) + 1e-9))


def _peak_density(np, spec, peak_ratio):
    # Fraction of bins whose magnitude exceeds peak_ratio * the global median.
    flat = spec.ravel()
    if flat.size == 0:
        return 0.0
    med = np.median(flat) + 1e-9
    peaks = np.count_nonzero(flat > peak_ratio * med)
    return peaks / flat.size


def _dct_chi2(np, arr):
    try:
        from scipy.fftpack import dct

        d = dct(dct(arr.T, norm="ortho").T, norm="ortho")
    except Exception:  # noqa: BLE001 - scipy missing or failed: skip
        return 0.0
    coeffs = d[1:, 1:].ravel()  # drop DC
    coeffs = coeffs[np.isfinite(coeffs)]
    if coeffs.size < 64:
        return 0.0
    coeffs = coeffs / (np.std(coeffs) + 1e-9)
    hist, edges = np.histogram(coeffs, bins=41, range=(-5, 5), density=True)
    centers = (edges[:-1] + edges[1:]) / 2
    # Laplacian model with the same scale.
    b = np.mean(np.abs(coeffs)) + 1e-9
    model = np.exp(-np.abs(centers) / b) / (2 * b)
    model = model / (model.sum() + 1e-9)
    hist = hist / (hist.sum() + 1e-9)
    chi2 = 0.5 * np.sum((hist - model) ** 2 / (hist + model + 1e-9))
    return float(chi2)


def _ratio(value, high):
    if high <= 0:
        return 0.0
    return float(min(1.0, max(0.0, value / high)))
