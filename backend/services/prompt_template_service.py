from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "data" / "prompt_templates.json"

DEFAULT_TEMPLATES = [
    {
        "id": "seed-photo-product",
        "title": "写实摄影：产品主图",
        "text": "Create a premium studio product photograph of a translucent smart speaker on a brushed aluminum surface, softbox lighting, crisp reflections, shallow depth of field, clean background, realistic materials, commercial advertising quality, no text, no logo.",
    },
    {
        "id": "seed-poster-typography",
        "title": "海报设计：字体排版",
        "text": "Design a vertical cinematic poster for a fictional art exhibition titled \"LIGHT ARCHIVE\", elegant editorial typography, strong visual hierarchy, one striking central image, museum-grade layout, balanced margins, refined color palette, print-ready composition.",
    },
    {
        "id": "seed-infographic",
        "title": "信息图：流程说明",
        "text": "Create a clean infographic explaining a four-step AI image workflow: prompt, reference, generation, refinement. Use clear English labels, simple icons, consistent spacing, modern SaaS visual style, high readability, white background, professional layout.",
    },
    {
        "id": "seed-character-consistency",
        "title": "角色设计：一致性设定",
        "text": "Generate a character reference sheet for an original young explorer, front view, side view, three facial expressions, consistent outfit and hairstyle, clean turnaround layout, neutral background, detailed fabric and accessory notes, polished concept art style.",
    },
    {
        "id": "seed-ui-mockup",
        "title": "界面设计：移动 App",
        "text": "Create a high-fidelity mobile app screen for a personal finance dashboard, dark mode, clear cards, transaction list, monthly spending chart, restrained accent colors, realistic iOS layout, readable text, production-ready UI mockup.",
    },
    {
        "id": "seed-game-asset",
        "title": "游戏素材：图标套组",
        "text": "Create a cohesive set of twelve fantasy RPG item icons, potions, rings, scrolls, gems, and keys, isometric view, transparent-feeling dark background, consistent lighting, sharp silhouettes, game-ready asset style.",
    },
    {
        "id": "seed-social-card",
        "title": "社媒图片：活动宣传",
        "text": "Design a square social media announcement graphic for a weekend design workshop, bold headline, supporting date and location text, layered paper texture, warm but professional colors, eye-catching composition, suitable for Instagram.",
    },
    {
        "id": "seed-local-edit",
        "title": "局部编辑：自然替换",
        "text": "Edit only the colored semi-transparent marked regions. Replace them with a realistic glass control panel, preserve the original camera angle, lighting direction, shadows, reflections, edge detail, and overall photographic style. Do not change unmarked areas.",
    },
    {
        "id": "seed-portrait-basketball-direct-flash",
        "title": "人物写真：球场直闪（Basketball Court Direct Flash Portrait）",
        "text": """Prompt:

35mm color film photography with harsh direct on-camera flash, specular highlights on skin and clothing, strong catchlights in eyes, high contrast flash illumination, authentic film grain and color shift, high fashion fresh innocent basketball court editorial style, intimate first-person low-angle POV shot from below, early 20s sexy Chinese female idol with ultra-realistic delicate refined Chinese features, seductive almond-shaped fox eyes with natural double eyelids, high nose bridge, small sharp V-shaped jawline, flawless realistic porcelain skin with cool ivory undertone and visible flash specular highlights, fine delicate skin texture with subtle pores micro details and natural dewy glow under flash, fresh natural sporty makeup with soft dewy glow, subtle natural flush on cheeks, natural pink lips slightly parted, subtle natural freckles across nose and cheeks, long dark brown hair tied in a high playful ponytail with some loose strands framing the face and realistic loose strands, wearing a loose white tank top and white high-waisted basketball shorts, white knee-high sports socks, seductive natural leaning pose against the basketball hoop pole on the outdoor court at dusk, body angled sideways with naturally arched back and hips gently pushed back to accentuate perky round hips and sexy butt curve, one leg naturally extended forward toward the camera and the other leg slightly bent to emphasize long sexy legs, both hands lightly resting on the basketball pole at shoulder height, intensely seductive playful yet pitiable doe-eyed gaze straight at the viewer with soft vulnerable longing eyes and a gentle teasing smile full of quiet temptation and desire, harsh direct on-camera flash creating sharp specular highlights and strong catchlights, background with blurred basketball court and hoop under dusk sky, high contrast film color grading with natural flash look, extremely sharp yet soft skin rendering with authentic 35mm direct flash aesthetic, natural hair strands, realistic fabric texture on tank top and shorts with socks detail, no plastic skin, no digital over-sharpening, no airbrushing, no blemishes, no moles, no oily skin, no watermark, no text, authentic 35mm direct flash film basketball court look --ar 9:16
Source: @BubbleBrain""",
    },
    {
        "id": "seed-portrait-korean-soft-mist",
        "title": "人物写真：韩系柔雾（Korean Editorial Portrait with Soft Mist）",
        "text": """Prompt:

9:16 vertical - editorial portrait, single subject soft black mist filter, subtle haze, gentle highlight bloom, muted tones minimal indoor space, clean background, slight texture young Korean woman, minimal makeup, natural skin texture outfit: fitted ribbed knit top or soft camisole layered under a loose shirt, paired with high-waisted shorts or skirt; fabric slightly clings to body shape, soft and natural, no revealing elements hair: slightly messy, natural volume pose: sitting on floor with one leg bent and the other relaxed, body slightly leaning, shoulders not aligned, head tilted composition: subject slightly off-center, negative space present expression: calm, slightly distant, natural lips lighting: soft side light, gentle shadow falloff mood: understated, quiet, subtly sensual through natural body lines, relaxed and unposed quality: fine grain, slight softness, realistic look
Source: @BubbleBrain""",
    },
    {
        "id": "seed-portrait-subway-candid",
        "title": "人物写真：地铁抓拍（Subway Candid Photo）",
        "text": """Prompt:

A beautiful woman looking at her phone on the subway; a candid photo.
Source: @AntCaveClub | @underwoodxie96""",
    },
]


class PromptTemplateServiceError(Exception):
    """Base error for prompt-template JSON persistence failures."""


class PromptTemplateValidationError(PromptTemplateServiceError, ValueError):
    """Raised when a template payload is invalid."""


class PromptTemplateNotFoundError(PromptTemplateServiceError, KeyError):
    """Raised when a requested template id does not exist."""


class PromptTemplateService:
    def __init__(self, template_path: str | Path | None = None):
        self.template_path = Path(template_path) if template_path else DEFAULT_TEMPLATE_PATH
        self._lock = threading.RLock()
        self._ensure_storage()

    def list_templates(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._read_store()["templates"]]

    def create_template(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            store = self._read_store()
            now = self._now()
            template = self._normalize_template(payload)
            template["id"] = str(payload.get("id") or uuid.uuid4().hex)
            template["created_at"] = now
            template["updated_at"] = now
            store["templates"].insert(0, template)
            self._write_store(store)
            return dict(template)

    def update_template(self, template_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            store = self._read_store()
            for index, current in enumerate(store["templates"]):
                if current["id"] == template_id:
                    merged = dict(current)
                    merged.update(payload or {})
                    updated = self._normalize_template(merged)
                    updated["id"] = current["id"]
                    updated["created_at"] = current.get("created_at", self._now())
                    updated["updated_at"] = self._now()
                    store["templates"][index] = updated
                    self._write_store(store)
                    return dict(updated)
        raise PromptTemplateNotFoundError(f"提示词模板不存在: {template_id}")

    def delete_template(self, template_id: str) -> None:
        with self._lock:
            store = self._read_store()
            next_templates = [item for item in store["templates"] if item["id"] != template_id]
            if len(next_templates) == len(store["templates"]):
                raise PromptTemplateNotFoundError(f"提示词模板不存在: {template_id}")
            store["templates"] = next_templates
            self._write_store(store)

    def _ensure_storage(self) -> None:
        with self._lock:
            self.template_path.parent.mkdir(parents=True, exist_ok=True)
            if not self.template_path.exists():
                now = self._now()
                self._write_store(
                    {
                        "templates": [
                            {**template, "created_at": now, "updated_at": now}
                            for template in DEFAULT_TEMPLATES
                        ]
                    }
                )

    def _read_store(self) -> dict[str, list[dict[str, Any]]]:
        try:
            raw = self.template_path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {"templates": []}
        except json.JSONDecodeError as exc:
            raise PromptTemplateServiceError(f"提示词模板 JSON 格式错误: {self.template_path}") from exc
        except OSError as exc:
            raise PromptTemplateServiceError(f"读取提示词模板失败: {self.template_path}") from exc

        if isinstance(data, list):
            data = {"templates": data}
        if not isinstance(data, dict) or not isinstance(data.get("templates"), list):
            raise PromptTemplateServiceError("提示词模板文件结构必须为 {'templates': [...]}")
        return {"templates": [dict(item) for item in data["templates"] if isinstance(item, dict)]}

    def _write_store(self, store: dict[str, Any]) -> None:
        payload = json.dumps(store, ensure_ascii=False, indent=2)
        tmp_path = self.template_path.with_suffix(f"{self.template_path.suffix}.tmp")
        try:
            self.template_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_text(payload + "\n", encoding="utf-8")
            tmp_path.replace(self.template_path)
        except OSError as exc:
            raise PromptTemplateServiceError(f"写入提示词模板失败: {self.template_path}") from exc

    @staticmethod
    def _normalize_template(payload: dict[str, Any]) -> dict[str, Any]:
        title = str(payload.get("title") or "").strip() or "未命名"
        text = str(payload.get("text") or "").strip()
        if not text:
            raise PromptTemplateValidationError("模板内容不能为空")
        return {"id": payload.get("id"), "title": title, "text": text}

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
