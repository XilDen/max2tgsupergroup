from __future__ import annotations

from collections.abc import Mapping
from typing import Any

MASK_SUFFIX = "…"


def mask_text(value: Any, visible: int = 2) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text[:visible] + MASK_SUFFIX


def mask_mapping_values(values: Mapping[Any, Any]) -> dict[Any, str]:
    return {key: mask_text(value) for key, value in values.items()}
