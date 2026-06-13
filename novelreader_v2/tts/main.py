from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger
from tqdm import tqdm

from novelreader_v2.common.config import load_config, project_path
from novelreader_v2.common.jsonio import read_jsonl, write_json
from novelreader_v2.common.schema import PlanItem


@dataclass(frozen=True)
class VoiceReference:
    speaker: str
    ref_audio: str = ""
    ref_text: str = ""
    x_vector_only_mode: bool = False
    custom_voice_speaker: str = ""


def _chunks(rows: list[PlanItem], size: int) -> list[list[PlanItem]]:
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


def _safe_cache_name(value: str | Path) -> str:
    stem = Path(str(value)).stem or "reference"
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
        raise RuntimeError("openai-whisper is not installed. Install it or set ref_text/default_ref_text.") from exc

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


def _resolve_path(path: str, project_dir: Path, refs_dir: str) -> str:
    path = path.strip()
    if not path:
        return ""
    candidate = Path(path)
    if candidate.is_absolute():
        return str(candidate)
    project_candidate = project_path(project_dir, path)
    if project_candidate.exists():
        return str(project_candidate)
    refs_candidate = project_path(project_dir, refs_dir) / path
    return str(refs_candidate)


def _resolve_ref_text(
    *,
    ref_audio: str,
    configured_ref_text: str,
    tts_config: dict[str, Any],
    project_dir: Path,
) -> str:
    if configured_ref_text.strip():
        return configured_ref_text.strip()

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
    return bool(re.fullmatch(r"[\s「」『』“”\"'‘’…。.，,、！？!?；;：:\-—]+", text.strip()))


def _write_silence(path: Path, sample_rate: int = 24000, duration_ms: int = 700) -> None:
    import numpy as np
    import soundfile as sf

    path.parent.mkdir(parents=True, exist_ok=True)
    samples = int(sample_rate * duration_ms / 1000)
    sf.write(path, np.zeros(samples, dtype="float32"), sample_rate)


def _plan_path(project_dir: Path, config: dict[str, Any], chapter_id: str) -> Path:
    return project_path(project_dir, config["planner"]["output_dir"]) / f"{chapter_id}.plan.jsonl"


def _speaker_voice_config(tts_config: dict[str, Any], speaker: str) -> dict[str, Any]:
    voice_refs = tts_config.get("voice_refs") or tts_config.get("speaker_refs") or {}
    if isinstance(voice_refs, dict):
        exact = voice_refs.get(speaker)
        if isinstance(exact, dict):
            return exact
        fallback = voice_refs.get("default")
        if isinstance(fallback, dict):
            return fallback
    return {}


def _voice_reference_for_item(item: PlanItem, tts_config: dict[str, Any], project_dir: Path) -> VoiceReference:
    voice_config = _speaker_voice_config(tts_config, item.speaker)
    refs_dir = str(tts_config.get("refs_dir", "refs"))
    ref_audio = str(voice_config.get("ref_audio") or tts_config.get("default_ref_audio", "")).strip()
    ref_audio = _resolve_path(ref_audio, project_dir, refs_dir)
    ref_text = str(voice_config.get("ref_text") or tts_config.get("default_ref_text", "")).strip()
    x_vector = bool(voice_config.get("x_vector_only_mode", tts_config.get("x_vector_only_mode", False)))
    custom_voice_speaker = str(
        voice_config.get("custom_voice_speaker")
        or voice_config.get("speaker")
        or tts_config.get("custom_voice_speaker", "Vivian")
    ).strip()
    return VoiceReference(
        speaker=item.speaker,
        ref_audio=ref_audio,
        ref_text=ref_text,
        x_vector_only_mode=x_vector,
        custom_voice_speaker=custom_voice_speaker,
    )


def _plain_text_for_tts(item: PlanItem, tts_config: dict[str, Any]) -> str:
    use_adapted = bool(tts_config.get("use_adapted_text", True))
    text = item.adapted_text.strip() if use_adapted and item.adapted_text.strip() else item.text.strip()
    return text


def _style_instruct(item: PlanItem, tts_config: dict[str, Any]) -> str:
    if not bool(tts_config.get("use_style_instruct", True)):
        return ""
    parts = []
    if item.emotion:
        parts.append(f"情绪：{item.emotion}")
    if item.intensity:
        parts.append(f"强度：{item.intensity}/5")
    if item.style_prompt:
        parts.append(item.style_prompt)
    if item.delivery:
        delivery = "，".join(f"{key}={value}" for key, value in item.delivery.items())
        if delivery:
            parts.append(f"表演细节：{delivery}")
    return "；".join(parts)


def _meta_for_item(item: PlanItem, synth_text: str, backend: str, voice: VoiceReference, output_path: Path) -> dict[str, Any]:
    return {
        "id": item.id,
        "chapter_id": item.chapter_id,
        "chunk_id": item.chunk_id,
        "speaker": item.speaker,
        "kind": item.kind,
        "text": item.text,
        "synth_text": synth_text,
        "adapted_text": item.adapted_text,
        "emotion": item.emotion,
        "intensity": item.intensity,
        "style_prompt": item.style_prompt,
        "delivery": item.delivery,
        "pause_after_ms": item.pause_after_ms,
        "confidence": item.confidence,
        "needs_review": item.needs_review,
        "evidence_ids": item.evidence_ids,
        "backend": backend,
        "voice_ref_audio": voice.ref_audio,
        "custom_voice_speaker": voice.custom_voice_speaker,
        "output_path": str(output_path),
    }


def _can_reuse_existing_audio(meta_path: Path, item: PlanItem, synth_text: str, backend: str) -> bool:
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return (
        int(meta.get("id", -1)) == item.id
        and str(meta.get("text", "")) == item.text
        and str(meta.get("synth_text", "")) == synth_text
        and str(meta.get("speaker", "")) == item.speaker
        and str(meta.get("backend", "")) == backend
    )


def _build_clone_prompts(
    model: Any,
    items: list[PlanItem],
    tts_config: dict[str, Any],
    project_dir: Path,
) -> dict[tuple[str, str, bool], Any]:
    prompts: dict[tuple[str, str, bool], Any] = {}
    for item in items:
        voice = _voice_reference_for_item(item, tts_config, project_dir)
        if not voice.ref_audio:
            raise RuntimeError("Set tts.default_ref_audio or tts.voice_refs.<speaker>.ref_audio before running clone TTS.")
        if voice.x_vector_only_mode:
            ref_text = ""
            x_vector = True
        else:
            ref_text = _resolve_ref_text(
                ref_audio=voice.ref_audio,
                configured_ref_text=voice.ref_text,
                tts_config=tts_config,
                project_dir=project_dir,
            )
            x_vector = not ref_text
        key = (voice.ref_audio, ref_text, x_vector)
        if key in prompts:
            continue
        logger.info("Building voice clone prompt: audio={}, x_vector_only={}", voice.ref_audio, x_vector)
        prompts[key] = model.create_voice_clone_prompt(
            ref_audio=voice.ref_audio,
            ref_text=ref_text,
            x_vector_only_mode=x_vector,
        )
    return prompts


def _preflight_tts_config(items: list[PlanItem], tts_config: dict[str, Any], project_dir: Path, backend: str) -> None:
    if backend not in {"qwen3", "qwen3_clone", "clone", "voice_clone"}:
        return
    missing = []
    for item in items:
        voice = _voice_reference_for_item(item, tts_config, project_dir)
        if not voice.ref_audio:
            missing.append(item.speaker)
    if missing:
        speakers = ", ".join(sorted(set(missing)))
        raise RuntimeError(
            "Set tts.default_ref_audio or tts.voice_refs.<speaker>.ref_audio before running clone TTS. "
            f"Missing speakers: {speakers}"
        )


def _generate_one(
    *,
    model: Any,
    item: PlanItem,
    backend: str,
    language: str,
    gen_kwargs: dict[str, Any],
    tts_config: dict[str, Any],
    project_dir: Path,
    clone_prompts: dict[tuple[str, str, bool], Any],
) -> tuple[list[Any], int, str, VoiceReference]:
    synth_text = _plain_text_for_tts(item, tts_config)
    voice = _voice_reference_for_item(item, tts_config, project_dir)

    if backend in {"qwen3", "qwen3_clone", "clone", "voice_clone"}:
        if voice.x_vector_only_mode:
            ref_text = ""
            x_vector = True
        else:
            ref_text = _resolve_ref_text(
                ref_audio=voice.ref_audio,
                configured_ref_text=voice.ref_text,
                tts_config=tts_config,
                project_dir=project_dir,
            )
            x_vector = not ref_text
        prompt = clone_prompts[(voice.ref_audio, ref_text, x_vector)]
        wavs, sr = model.generate_voice_clone(
            text=synth_text,
            language=language,
            voice_clone_prompt=prompt,
            **gen_kwargs,
        )
        return wavs, sr, synth_text, voice

    if backend in {"qwen3_voice_design", "voice_design"}:
        instruct = _style_instruct(item, tts_config) or "自然、清晰地朗读。"
        wavs, sr = model.generate_voice_design(
            text=synth_text,
            language=language,
            instruct=instruct,
            **gen_kwargs,
        )
        return wavs, sr, synth_text, voice

    if backend in {"qwen3_custom_voice", "custom_voice"}:
        instruct = _style_instruct(item, tts_config)
        wavs, sr = model.generate_custom_voice(
            text=synth_text,
            language=language,
            speaker=voice.custom_voice_speaker,
            instruct=instruct,
            **gen_kwargs,
        )
        return wavs, sr, synth_text, voice

    raise ValueError(f"Unsupported tts.backend: {backend}")


def run(project_dir: Path, config_path: Path | None = None, chapter: str | None = None) -> Path:
    if not chapter:
        raise ValueError("tts requires --chapter")

    config = load_config(project_dir, config_path)
    tts_config = config["tts"]
    plan_items = [PlanItem.model_validate(row) for row in read_jsonl(_plan_path(project_dir, config, chapter))]
    max_items = int(tts_config.get("max_items", 0))
    if max_items > 0:
        logger.info("Limiting TTS to first {} plan items", max_items)
        plan_items = plan_items[:max_items]

    output_dir = project_path(project_dir, tts_config["output_dir"]) / chapter
    output_dir.mkdir(parents=True, exist_ok=True)
    meta_dir = output_dir / "meta"
    meta_dir.mkdir(parents=True, exist_ok=True)

    backend = str(tts_config.get("backend", "qwen3_clone")).strip().lower()
    _preflight_tts_config(plan_items, tts_config, project_dir, backend)
    model = _load_qwen_model(tts_config)
    gen_kwargs = _generation_kwargs(tts_config)
    batch_size = int(tts_config.get("batch_size", 1))
    language = str(tts_config.get("language", "Chinese"))
    sample_rate = int(config["audio"].get("sample_rate", 24000))
    overwrite = bool(tts_config.get("overwrite", False))
    write_metadata = bool(tts_config.get("write_metadata", True))
    clone_prompts: dict[tuple[str, str, bool], Any] = {}
    if backend in {"qwen3", "qwen3_clone", "clone", "voice_clone"}:
        clone_prompts = _build_clone_prompts(model, plan_items, tts_config, project_dir)

    stats = {
        "success": 0,
        "skipped": 0,
        "silence": 0,
        "failed": 0,
        "total_time": 0.0,
        "backend": backend,
        "items": len(plan_items),
    }

    for batch in tqdm(_chunks(plan_items, max(1, batch_size)), desc=f"tts {chapter}"):
        pending: list[tuple[PlanItem, Path]] = []
        for item in batch:
            output_path = output_dir / f"{item.id:04d}.wav"
            meta_path = meta_dir / f"{item.id:04d}.json"
            synth_text = _plain_text_for_tts(item, tts_config)
            if output_path.exists() and not overwrite:
                if _can_reuse_existing_audio(meta_path, item, synth_text, backend):
                    stats["skipped"] += 1
                    continue
                logger.info("Regenerating stale audio part: {}", output_path)
            pending.append((item, output_path))
        if not pending:
            continue

        started = time.time()
        try:
            import soundfile as sf

            for item, output_path in pending:
                synth_text = _plain_text_for_tts(item, tts_config)
                if _is_silence_text(synth_text):
                    _write_silence(output_path, sample_rate=sample_rate, duration_ms=max(100, item.pause_after_ms))
                    stats["silence"] += 1
                    stats["success"] += 1
                    if write_metadata:
                        voice = _voice_reference_for_item(item, tts_config, project_dir)
                        write_json(meta_dir / f"{item.id:04d}.json", _meta_for_item(item, synth_text, backend, voice, output_path))
                    continue

                wavs, sr, used_text, voice = _generate_one(
                    model=model,
                    item=item,
                    backend=backend,
                    language=language,
                    gen_kwargs=gen_kwargs,
                    tts_config=tts_config,
                    project_dir=project_dir,
                    clone_prompts=clone_prompts,
                )
                sf.write(output_path, wavs[0], sr)
                stats["success"] += 1
                if write_metadata:
                    write_json(meta_dir / f"{item.id:04d}.json", _meta_for_item(item, used_text, backend, voice, output_path))
        except Exception as exc:
            stats["failed"] += len(pending)
            logger.error("TTS batch failed: {}", exc)
        finally:
            stats["total_time"] += time.time() - started

    (output_dir / "tts_stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        "TTS complete: {} success, {} skipped, {} silence, {} failed",
        stats["success"],
        stats["skipped"],
        stats["silence"],
        stats["failed"],
    )
    return output_dir
