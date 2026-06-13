from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "configs" / "default.yaml"


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(project_dir: Path, config_path: Path | None = None) -> dict[str, Any]:
    base = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8")) or {}
    local_path = config_path or project_dir / "config.yaml"
    if local_path.exists():
        override = yaml.safe_load(local_path.read_text(encoding="utf-8")) or {}
        base = deep_merge(base, override)
    return base


def project_path(project_dir: Path, relative: str | Path) -> Path:
    return project_dir / Path(relative)

