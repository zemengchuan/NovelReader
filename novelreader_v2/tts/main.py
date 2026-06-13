from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from loguru import logger
from tqdm import tqdm

from novelreader_v2.common.config import load_config, project_path
from novelreader_v2.common.jsonio import read_jsonl
from novelreader_v2.common.schema import PlanItem


def _chunks(rows: list[dict], size: int) -> list[list[dict]]:
    return [rows[i : i + size] for i in range(0, len(rows), size)]


def _torch_dtype(name: str):
    import torch

    mapping = {
        "float16": torch.float16,
        "fp16": torch.float16,
        "bfloat16": torch.bfloat16,
        "bf16": torch.bfloat16,
        "float32": torch.float32,
        "fp32": torch.float32,
    }
    return mapping.get(name.lower(), torch.bfloat16)


def _load_qwen_model(config: dict[str, Any]):
    try:
        import torch
        from qwen_tts import Qwen3TTSModel
    except ImportError as exc:
        raise RuntimeError(
            "qwen-tts is not installed. Activate the TTS environment and run: python -m pip install -U qwen-tts"
        ) from exc

    kwargs: dict[str, Any] = {
        "device_map": config.get("device", "cuda:0"),
        "dtype": _torch_dtype(str(config.get("dtype", "bfloat16"))),
    }
    attn = str(config.get("attn_implementation", "")).strip()
    if attn:
        kwargs["attn_implementation"] = attn

    logger.info("Loading Qwen3-TTS model: {}", config["model_dir"])
    logger.info("Torch CUDA available: {}", torch.cuda.is_available())
    return Qwen3TTSModel.from_pretrained(config["model_dir"], **kwargs)


def _safe_cache_name(ref_audio: str | Path) -> str:
    stem = Path(str(ref_audio)).stem or "reference"
    return re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", stem).strip("_") or "reference"


def _asr_device(tts_config: dict[str, Any]) -> str:
    configured = str(tts_config.get("asr_device", "auto")).strip().lower()
    if configured and configured != "auto":
        return configured
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def _transcribe_with_whisper(ref_audio: str | Path, tts_config: dict[str, Any]) -> str:
    try:
        import whisper
    except ImportError as exc:
        raise RuntimeError("openai-whisper is not installed. Install it or set tts.default_ref_text.") from exc

    model_name = str(tts_config.get("whisper_model", "small"))
    language = str(tts_config.get("asr_language", "zh")).strip()
    device = _asr_device(tts_config)
    logger.info("Transcribing reference audio with Whisper model={}, device={}", model_name, device)
    model = whisper.load_model(model_name, device=device)
    kwargs: dict[str, Any] = {"fp16": device.startswith("cuda")}
    if language:
        kwargs["language"] = language
    result = model.transcribe(str(ref_audio), **kwargs)
    text = str(result.get("text", "")).strip()
    if not text:
        raise RuntimeError(f"Whisper returned empty transcript for {ref_audio}")
    return text


def _resolve_ref_text(ref_audio: str | Path, tts_config: dict[str, Any], project_dir: Path) -> str:
    configured = str(tts_config.get("default_ref_text", "")).strip()
    if configured:
        return configured

    cache_dir = project_path(project_dir, str(tts_config.get("ref_text_cache_dir", "cache/ref_text")))
    cache_path = cache_dir / f"{_safe_cache_name(ref_audio)}.txt"
    if cache_path.exists():
        text = cache_path.read_text(encoding="utf-8").strip()
        if text:
            logger.info("Loaded cached reference transcript: {}", cache_path)
            return text

    if not bool(tts_config.get("auto_transcribe_ref", True)):
        return ""
    if str(tts_config.get("asr_backend", "whisper")).lower() != "whisper":
        raise RuntimeError("Only tts.asr_backend=whisper is currently implemented.")

    text = _transcribe_with_whisper(ref_audio, tts_config)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(text, encoding="utf-8")
    logger.info("Wrote reference transcript cache: {}", cache_path)
    return text


def _generation_kwargs(tts_config: dict[str, Any]) -> dict[str, Any]:
    return {
        "max_new_tokens": int(tts_config.get("max_new_tokens", 2048)),
        "do_sample": True,
        "top_k": int(tts_config.get("top_k", 50)),
        "top_p": float(tts_config.get("top_p", 1.0)),
        "temperature": float(tts_config.get("temperature", 0.9)),
        "repetition_penalty": float(tts_config.get("repetition_penalty", 1.05)),
        "subtalker_dosample": True,
        "subtalker_top_k": int(tts_config.get("top_k", 50)),
        "subtalker_top_p": float(tts_config.get("top_p", 1.0)),
        "subtalker_temperature": float(tts_config.get("temperature", 0.9)),
    }


def _is_silence_text(text: str) -> bool:
    return bool(re.fullmatch(r"[「」『』“”'\"\s…。，、.!！？?]+", text.strip()))


def _write_silence(path: Path, sample_rate: int = 24000, duration_ms: int = 700) -> None:
    import numpy as np
    import soundfile as sf

    path.parent.mkdir(parents=True, exist_ok=True)
    samples = int(sample_rate * duration_ms / 1000)
    sf.write(path, np.zeros(samples, dtype="float32"), sample_rate)


def _plan_path(project_dir: Path, config: dict[str, Any], chapter_id: str) -> Path:
    return project_path(project_dir, config["planner"]["output_dir"]) / f"{chapter_id}.plan.jsonl"


def run(project_dir: Path, config_path: Path | None = None, chapter: str | None = None) -> Path:
    if not chapter:
        raise ValueError("tts requires --chapter")

    config = load_config(project_dir, config_path)
    tts_config = config["tts"]
    plan_items = [PlanItem.model_validate(row).model_dump(mode="json") for row in read_jsonl(_plan_path(project_dir, config, chapter))]
    max_items = int(tts_config.get("max_items", 0))
    if max_items > 0:
        logger.info("Limiting TTS to first {} plan items", max_items)
        plan_items = plan_items[:max_items]

    output_dir = project_path(project_dir, tts_config["output_dir"]) / chapter
    output_dir.mkdir(parents=True, exist_ok=True)

    ref_audio = str(tts_config.get("default_ref_audio", "")).strip()
    if not ref_audio:
        raise RuntimeError("Set tts.default_ref_audio before running TTS.")

    model = _load_qwen_model(tts_config)
    ref_text = _resolve_ref_text(ref_audio, tts_config, project_dir)
    prompt = model.create_voice_clone_prompt(
        ref_audio=ref_audio,
        ref_text=ref_text,
        x_vector_only_mode=bool(tts_config.get("x_vector_only_mode", False)) or not ref_text,
    )
    gen_kwargs = _generation_kwargs(tts_config)
    batch_size = int(tts_config.get("batch_size", 1))
    language = str(tts_config.get("language", "Chinese"))
    stats = {"success": 0, "skipped": 0, "failed": 0, "total_time": 0.0}

    for batch in tqdm(_chunks(plan_items, batch_size), desc=f"tts {chapter}"):
        pending: list[tuple[dict, Path]] = []
        for item in batch:
            output_path = output_dir / f"{int(item['id']):04d}.wav"
            if output_path.exists():
                stats["skipped"] += 1
                continue
            pending.append((item, output_path))
        if not pending:
            continue

        started = time.time()
        try:
            import soundfile as sf

            texts = [item["text"] for item, _ in pending]
            for text, (_, output_path) in zip(texts, pending):
                if _is_silence_text(text):
                    _write_silence(output_path)
                    stats["success"] += 1
                    continue

                wavs, sr = model.generate_voice_clone(
                    text=text,
                    language=language,
                    voice_clone_prompt=prompt,
                    **gen_kwargs,
                )
                sf.write(output_path, wavs[0], sr)
                stats["success"] += 1
        except Exception as exc:
            stats["failed"] += len(pending)
            logger.error("TTS batch failed: {}", exc)
        finally:
            stats["total_time"] += time.time() - started

    (output_dir / "tts_stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("TTS complete: {} success, {} skipped, {} failed", stats["success"], stats["skipped"], stats["failed"])
    return output_dir

