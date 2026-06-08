"""Metadata analyzer (high-confidence rule).

Scans EXIF, PNG text chunks, ICC profile description and XMP for known
AI-tool keywords. A hit is treated as high-confidence AI. Only needs Pillow.
"""

from __future__ import annotations

import io
from typing import Any


def analyze(image_bytes: bytes, cfg: dict[str, Any]) -> dict[str, Any]:
    try:
        from PIL import Image
    except ImportError:
        return {"score": None, "hit": False, "signals": {}, "evidence": [], "error": "Pillow 未安装"}

    keywords = [k.lower() for k in cfg.get("metadata_keywords", [])]
    haystack_parts: list[str] = []

    try:
        img = Image.open(io.BytesIO(image_bytes))
        # PNG text chunks / generic info (SD stores params under 'parameters').
        for key, value in (img.info or {}).items():
            haystack_parts.append(str(key))
            haystack_parts.append(str(value))
        # EXIF — top-level IFD0 plus the Exif/GPS sub-IFDs, where free-text
        # fields such as UserComment live (not reached by exif.values()).
        try:
            exif = img.getexif()
            for v in exif.values():
                haystack_parts.append(str(v))
            for ifd_tag in (0x8769, 0x8825):  # Exif IFD, GPS IFD
                try:
                    for v in exif.get_ifd(ifd_tag).values():
                        haystack_parts.append(str(v))
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
        # ICC profile description
        icc = img.info.get("icc_profile")
        if icc:
            try:
                haystack_parts.append(icc.decode("latin-1", errors="ignore"))
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        return {"score": None, "hit": False, "signals": {}, "evidence": [], "error": str(exc)}

    haystack = "\n".join(haystack_parts).lower()
    matched = [kw for kw in keywords if kw and kw in haystack]

    if matched:
        return {
            "score": 1.0,
            "hit": True,
            "signals": {"matched_keywords": matched},
            "evidence": [f"元数据命中 AI 关键词: {', '.join(matched[:5])}"],
            "error": None,
        }
    return {"score": 0.0, "hit": False, "signals": {"matched_keywords": []}, "evidence": [], "error": None}
