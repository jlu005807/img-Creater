"""Load and validate detection config (thresholds + weights).

All tunable knobs live in ``config.json`` next to this file so the detector
can be iterated on without touching code. Missing keys fall back to the
built-in defaults below, so a partial/old config never breaks the module.
"""

from __future__ import annotations

import json
import os
from typing import Any

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

DEFAULTS: dict[str, Any] = {
    "weights": {"frequency": 0.3, "noise": 0.25, "jpeg": 0.25, "color": 0.2},
    "thresholds": {"ai": 0.6, "suspicious": 0.3},
    "metadata_keywords": [
        "stable diffusion", "midjourney", "dall-e", "dalle", "openai",
        "comfyui", "automatic1111", "ai generated", "synthetic", "gpt-image",
    ],
    "frequency": {
        "block_size": 256, "block_overlap": 0.5, "max_blocks": 12,
        "peak_ratio": 4.0, "peak_density_high": 0.02,
        "radial_deviation_high": 0.35, "dct_chi2_high": 0.5,
    },
    "noise": {
        "wavelet": "haar", "glcm_levels": 16,
        "glcm_correlation_low": 0.15, "residual_kurtosis_low": 1.5,
    },
    "jpeg": {
        "qtable_distance_high": 0.4, "recompress_qualities": [70, 80, 90, 95],
        "recompress_error_low": 1.5,
    },
    "color": {
        "gray_world_shift_high": 0.18, "cbcr_quantization_high": 0.4,
        "shadow_highlight_noise_ratio_low": 0.15,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | None = None) -> dict[str, Any]:
    """Return the merged config (file over defaults). Never raises on a bad
    file — falls back to defaults so detection stays available."""
    target = path or _CONFIG_PATH
    try:
        with open(target, "r", encoding="utf-8") as fh:
            user = json.load(fh)
        if not isinstance(user, dict):
            return dict(DEFAULTS)
        return _deep_merge(DEFAULTS, user)
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULTS)
