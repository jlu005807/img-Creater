"""Strongly-decoupled AI-generated-image detection module (beta).

Pure traditional signal-processing + statistics — no deep-learning models,
no GPU. The only public entry point is :func:`detect_image`. The module does
not import anything from the host application; the host calls it one-way.

Optional heavy dependencies (numpy/scipy/PyWavelets/invisible-watermark) are
lazily probed; when missing, detection reports "unavailable" instead of
raising, so the host app keeps working without them.
"""

from .detector import detect_image, detector_health

__all__ = ["detect_image", "detector_health"]
