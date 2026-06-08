"""Probe optional dependencies without importing them at module load.

The host app must be able to import ``detection`` even when none of the heavy
libraries are installed. We check availability via importlib.util.find_spec
(no actual import / side effects) and report what's missing so the API can
return a clear "install these" message.
"""

from __future__ import annotations

import importlib.util

# Map: pip/import name -> what it enables. Pillow is required by the host
# already, but list it so a stripped environment still reports clearly.
_REQUIRED = {
    "numpy": "数值计算",
    "PIL": "图像读取 (Pillow)",
}
_OPTIONAL = {
    "scipy": "频域/统计分析",
    "pywt": "小波噪声分析 (PyWavelets)",
    "imwatermark": "隐形水印解码 (invisible-watermark)",
}


def _has(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def missing_required() -> list[str]:
    return [name for name in _REQUIRED if not _has(name)]


def missing_optional() -> list[str]:
    return [name for name in _OPTIONAL if not _has(name)]


def is_available() -> bool:
    """Detection can run at all only if the required core (numpy + Pillow) is
    present. Optional libs just disable individual analyzers, not the whole
    pipeline."""
    return not missing_required()


def capability_report() -> dict:
    return {
        "available": is_available(),
        "missing_required": missing_required(),
        "missing_optional": missing_optional(),
        "analyzers": {
            "watermark": _has("imwatermark") and _has("PIL"),
            "metadata": _has("PIL"),
            "frequency": _has("numpy") and _has("PIL"),
            "noise": _has("numpy") and _has("pywt") and _has("PIL"),
            "jpeg": _has("numpy") and _has("PIL"),
            "color": _has("numpy") and _has("PIL"),
        },
    }
