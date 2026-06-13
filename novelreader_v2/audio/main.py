from __future__ import annotations

from pathlib import Path

from loguru import logger

from novelreader_v2.common.config import load_config, project_path
from novelreader_v2.common.jsonio import read_jsonl
from novelreader_v2.common.schema import PlanItem


def _plan_path(project_dir: Path, config: dict, chapter_id: str) -> Path:
    return project_path(project_dir, config["planner"]["output_dir"]) / f"{chapter_id}.plan.jsonl"


def run(project_dir: Path, config_path: Path | None = None, chapter: str | None = None) -> Path:
    if not chapter:
        raise ValueError("audio requires --chapter")

    import numpy as np
    import soundfile as sf

    config = load_config(project_dir, config_path)
    sample_rate = int(config["audio"].get("sample_rate", 24000))
    tts_dir = project_path(project_dir, config["tts"]["output_dir"]) / chapter
    output_dir = project_path(project_dir, config["audio"]["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    items = [PlanItem.model_validate(row) for row in read_jsonl(_plan_path(project_dir, config, chapter))]
    parts: list[np.ndarray] = []
    for item in items:
        wav_path = tts_dir / f"{item.id:04d}.wav"
        if not wav_path.exists():
            logger.warning("Missing audio part: {}", wav_path)
            continue
        wav, sr = sf.read(wav_path, dtype="float32")
        if sr != sample_rate:
            logger.warning("Sample rate mismatch for {}: {} != {}", wav_path, sr, sample_rate)
        if wav.ndim > 1:
            wav = wav.mean(axis=1)
        parts.append(wav)
        pause_samples = int(sample_rate * max(0, item.pause_after_ms) / 1000)
        if pause_samples:
            parts.append(np.zeros(pause_samples, dtype="float32"))

    if not parts:
        raise RuntimeError(f"No audio parts found in {tts_dir}")

    final = np.concatenate(parts)
    output_path = output_dir / f"{chapter}.wav"
    sf.write(output_path, final, sample_rate)
    logger.info("Wrote merged audio: {}", output_path)
    return output_path

