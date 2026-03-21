"""Qoder 模型目录读取与解析。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class QoderModelInfo:
    """Qoder 模型配置。"""

    id: str
    display_name: str
    scene: str
    max_input_tokens: int = 180000
    max_output_tokens: int = 32768
    is_default: bool = False
    is_reasoning: bool = False
    price_factor: float = 0.0

    @property
    def public_id(self) -> str:
        return _normalize_public_id(self.id, self.display_name)


_DISPLAY_NAME_OVERRIDES = {"mmodel": "MiniMax-M2.7"}
_PUBLIC_ID_OVERRIDES = {"mmodel": "MiniMax M2.7"}


def _normalize_public_id(model_id: str, display_name: str) -> str:
    override = _PUBLIC_ID_OVERRIDES.get(model_id)
    if override:
        return override

    candidate = display_name or model_id
    normalized = " ".join(candidate.replace("_", " ").replace("-", " ").split())
    return normalized or model_id


_FALLBACK_MODELS = [
    QoderModelInfo(id="auto", display_name="Auto", scene="chat", is_default=True, price_factor=1.0),
    QoderModelInfo(id="ultimate", display_name="Ultimate", scene="chat", is_reasoning=True, price_factor=1.6),
    QoderModelInfo(id="performance", display_name="Performance", scene="chat", price_factor=1.1),
    QoderModelInfo(id="efficient", display_name="Efficient", scene="chat", price_factor=0.3),
    QoderModelInfo(id="lite", display_name="Lite", scene="chat", price_factor=0.0),
    QoderModelInfo(id="qmodel", display_name="Qwen-Coder-Qoder-1.0", scene="chat", price_factor=0.2),
    QoderModelInfo(id="q35model", display_name="Qwen3.5-Plus", scene="chat", price_factor=0.2),
    QoderModelInfo(id="gmodel", display_name="GLM-5", scene="chat", price_factor=0.5),
    QoderModelInfo(id="kmodel", display_name="Kimi-K2.5", scene="chat", price_factor=0.3),
    QoderModelInfo(id="mmodel", display_name="MiniMax-M2.7", scene="chat", price_factor=0.2),
]


def _normalize_display_name(model_id: str, display_name: str) -> str:
    return _DISPLAY_NAME_OVERRIDES.get(model_id, display_name)


def load_qoder_models(qoder_home: Path, scene: str = "chat") -> list[QoderModelInfo]:
    """从 `.qoder/.auth/models` 读取模型列表。"""
    models_file = qoder_home / ".auth" / "models"
    if not models_file.exists():
        return list(_FALLBACK_MODELS)

    try:
        raw = json.loads(models_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return list(_FALLBACK_MODELS)

    scene_items = raw.get(scene) or raw.get("assistant") or []
    if not isinstance(scene_items, list):
        return list(_FALLBACK_MODELS)

    parsed_models: list[QoderModelInfo] = []
    for item in scene_items:
        if not isinstance(item, dict):
            continue

        model_key = str(item.get("key") or "").strip()
        if not model_key:
            continue

        parsed_models.append(
            QoderModelInfo(
                id=model_key,
                display_name=_normalize_display_name(model_key, str(item.get("display_name") or model_key)),
                scene=scene,
                max_input_tokens=int(item.get("max_input_tokens") or 180000),
                max_output_tokens=int(item.get("max_output_tokens") or 32768),
                is_default=bool(item.get("is_default")),
                is_reasoning=bool(item.get("is_reasoning")),
                price_factor=float(item.get("price_factor") or 0.0),
            )
        )

    return parsed_models or list(_FALLBACK_MODELS)


def _register_alias(aliases: dict[str, str], alias: str, target: str) -> None:
    normalized = alias.strip().lower()
    if normalized:
        aliases[normalized] = target


def build_model_aliases(models: list[QoderModelInfo]) -> dict[str, str]:
    """构建外部模型名到 Qoder 内部 key 的映射。"""
    aliases: dict[str, str] = {}
    for model in models:
        for alias in (
            model.id,
            model.display_name,
            model.public_id,
            f"qoder/{model.id}",
            f"qoder/{model.display_name}",
            f"qoder/{model.public_id}",
        ):
            _register_alias(aliases, alias, model.id)
    return aliases


def resolve_model_name(
    requested_model: str | None,
    models: list[QoderModelInfo],
    default_model: str,
) -> str:
    """把外部请求里的 model 名映射到 Qoder 可识别的值。"""
    aliases = build_model_aliases(models)
    candidate = (requested_model or "").strip().lower()
    if not candidate:
        return default_model

    if candidate in aliases:
        return aliases[candidate]

    available = ", ".join(dict.fromkeys(model.public_id for model in models))
    raise ValueError(f"不支持的 model: {requested_model}；可选值: {available}")
