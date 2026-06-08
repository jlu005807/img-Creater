"""Metadata analyzer (high-confidence rule).

Scans image metadata for AI-generation provenance. To avoid false positives,
evidence is split into two tiers:

  * STRONG (high-confidence hit -> verdict "ai"): a structured generation
    field is present — A1111/SD ``parameters``, ComfyUI ``workflow``,
    InvokeAI ``sd-metadata``/``Dream`` text chunks, or a known tool keyword in
    the EXIF/``Software`` provenance field.
  * WEAK (NOT a hit): a keyword appears only in free text (ICC description,
    generic EXIF, XMP, etc.). This is surfaced as evidence for transparency
    but does not by itself decide the verdict — a stray "synthetic"/"firefly"
    substring in a real photo must not flip the result to AI.

Only needs Pillow.
"""

from __future__ import annotations

import io
from typing import Any

# PNG/info text-chunk keys whose mere presence is a definitive AI-generation
# marker (compared case-insensitively).
_STRONG_KEYS = {"parameters", "workflow", "sd-metadata", "dream"}
# Binary blobs in img.info that must not be string-scanned as free text.
_SKIP_INFO_KEYS = {"icc_profile", "exif"}
_SOFTWARE_TAG = 0x0131  # EXIF Software


def analyze(image_bytes: bytes, cfg: dict[str, Any]) -> dict[str, Any]:
    try:
        from PIL import Image
    except ImportError:
        return {"score": None, "hit": False, "signals": {}, "evidence": [], "error": "Pillow 未安装"}

    keywords = [k.lower() for k in cfg.get("metadata_keywords", [])]
    strong_parts: list[str] = []   # structured provenance fields
    weak_parts: list[str] = []     # free-text / generic fields
    present_strong_keys: list[str] = []

    try:
        img = Image.open(io.BytesIO(image_bytes))
        info = img.info or {}
        for key, value in info.items():
            kl = str(key).lower()
            if kl in _SKIP_INFO_KEYS:
                continue  # binary blobs handled separately below
            if kl in _STRONG_KEYS:
                present_strong_keys.append(str(key))
                strong_parts.append(str(value))
            elif kl == "software":
                strong_parts.append(str(value))
            else:
                weak_parts.append(str(key))
                weak_parts.append(str(value))

        # EXIF: Software (0x0131) is structured provenance; everything else
        # (incl. Exif/GPS sub-IFD free text such as UserComment) is weak.
        try:
            exif = img.getexif()
            software = exif.get(_SOFTWARE_TAG)
            if software:
                strong_parts.append(str(software))
            for tag, v in exif.items():
                if tag != _SOFTWARE_TAG:
                    weak_parts.append(str(v))
            for ifd_tag in (0x8769, 0x8825):  # Exif IFD, GPS IFD
                try:
                    for v in exif.get_ifd(ifd_tag).values():
                        weak_parts.append(str(v))
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

        # ICC profile description — free text.
        icc = info.get("icc_profile")
        if icc:
            try:
                weak_parts.append(icc.decode("latin-1", errors="ignore"))
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        return {"score": None, "hit": False, "signals": {}, "evidence": [], "error": str(exc)}

    strong_hay = "\n".join(strong_parts).lower()
    strong_kw = [kw for kw in keywords if kw and kw in strong_hay]

    # STRONG: a known generation field is present, or a keyword sits inside a
    # structured provenance field -> high-confidence AI.
    if present_strong_keys or strong_kw:
        detail = ", ".join((present_strong_keys + strong_kw)[:5])
        return {
            "score": 1.0,
            "hit": True,
            "signals": {"strong_keys": present_strong_keys, "matched_keywords": strong_kw},
            "evidence": [f"元数据含 AI 生成标记: {detail}"],
            "error": None,
        }

    # WEAK: keyword only in free text -> surface it, but do NOT declare AI.
    weak_hay = "\n".join(weak_parts).lower()
    weak_kw = [kw for kw in keywords if kw and kw in weak_hay]
    if weak_kw:
        return {
            "score": 0.0,
            "hit": False,
            "signals": {"weak_keywords": weak_kw},
            "evidence": [f"自由文本中出现关键词（弱信号，未据此判定）: {', '.join(weak_kw[:5])}"],
            "error": None,
        }

    return {"score": 0.0, "hit": False, "signals": {"matched_keywords": []}, "evidence": [], "error": None}
