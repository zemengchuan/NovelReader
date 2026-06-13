from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger
from tqdm import tqdm

from novelreader_v2.common.config import load_config, project_path
from novelreader_v2.common.jsonio import read_json, read_jsonl, write_jsonl
from novelreader_v2.common.ollama import ollama_generate_json
from novelreader_v2.common.schema import CharacterBible, Chapter, ChapterState, PlanItem


PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "speaker": {"type": "string"},
                    "kind": {"type": "string", "enum": ["narration", "dialogue"]},
                    "emotion": {"type": "string"},
                    "style_prompt": {"type": "string"},
                    "delivery": {
                        "type": "object",
                        "additionalProperties": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "number"},
                                {"type": "boolean"},
                                {"type": "array", "items": {"type": "string"}},
                            ]
                        },
                    },
                    "pause_after_ms": {"type": "integer"},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                    "needs_review": {"type": "boolean"},
                },
                "required": [
                    "text",
                    "speaker",
                    "kind",
                    "emotion",
                    "style_prompt",
                    "pause_after_ms",
                    "confidence",
                    "reason",
                    "needs_review",
                ],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["items", "summary"],
}


SYSTEM_PROMPT = """你是中文小说有声书导演，只输出 JSON。
任务：根据 target_text 生成朗读计划。

你负责文学理解：
- 自己划分朗读单元。
- 自己判断 speaker。
- 自己判断旁白、对白、内心独白、语气词、沉默。
- 自己根据人物卡和上下文写 emotion/style_prompt/delivery。

硬性规则：
- 只能输出 target_text 中的原文片段；context_before/context_after 只能用于理解，不得输出。
- text 必须是 target_text 中连续出现的原文，不要改写、概括、新增。
- speaker 优先用 Character Bible 中的人物名；不确定时可用“旁白”“未具名男声”“未具名女声”“未知角色”。
- kind 只能是 narration 或 dialogue。内心独白、语气词、喊叫、沉默都归入 dialogue，并在 emotion/style_prompt 中说明。
- style_prompt 必须具体，避免“自然中文对白”这种模板话。
- 每条必须有 confidence 和 reason；不确定就 needs_review=true。
- summary 用一句话概括本 chunk 结束时的上下文状态，供下一 chunk 使用。
- 不要输出 Markdown，不要解释。"""


@dataclass(frozen=True)
class TextChunk:
    chunk_id: int
    start: int
    end: int
    context_before: str
    target_text: str
    context_after: str


def _selected_chapters(chapters: list[Chapter], chapter: str | None) -> list[Chapter]:
    if not chapter:
        return chapters
    rows = [row for row in chapters if row.chapter_id == chapter]
    if not rows:
        raise ValueError(f"chapter {chapter!r} not found")
    return rows


def _snap_forward(text: str, position: int, limit: int) -> int:
    if position >= len(text):
        return len(text)
    stop = min(len(text), position + limit)
    for index in range(position, stop):
        if text[index] in "\n。！？!?；;」”":
            return index + 1
    return position


def _build_chunks(text: str, target_chars: int, overlap_chars: int, snap_limit: int) -> list[TextChunk]:
    chunks: list[TextChunk] = []
    start = 0
    chunk_id = 1
    while start < len(text):
        rough_end = min(len(text), start + target_chars)
        end = _snap_forward(text, rough_end, snap_limit) if rough_end < len(text) else len(text)
        if end <= start:
            end = rough_end
        chunks.append(
            TextChunk(
                chunk_id=chunk_id,
                start=start,
                end=end,
                context_before=text[max(0, start - overlap_chars) : start],
                target_text=text[start:end],
                context_after=text[end : min(len(text), end + overlap_chars)],
            )
        )
        start = end
        chunk_id += 1
    return chunks


def _bible_brief(bible: CharacterBible) -> str:
    rows = []
    for name, profile in bible.characters.items():
        rows.append(
            {
                "name": name,
                "aliases": profile.aliases[:5],
                "gender": profile.gender,
                "identity": profile.identity,
                "personality": profile.personality[:5],
                "speech_style": profile.speech_style,
                "voice_style": profile.voice_style,
                "relations": dict(list(profile.relations.items())[:5]),
            }
        )
    return json.dumps({"characters": rows, "aliases": bible.aliases}, ensure_ascii=False)


def _candidate_speakers(bible: CharacterBible, narrator: str) -> set[str]:
    names = {narrator, "未具名男声", "未具名女声", "未知角色"}
    names.update(bible.characters.keys())
    return names


def _state_text(state: ChapterState | None) -> str:
    if not state:
        return "无"
    return json.dumps(state.model_dump(mode="json"), ensure_ascii=False)


def _make_prompt(
    chapter: Chapter,
    chunk: TextChunk,
    bible: CharacterBible,
    state: ChapterState | None,
    previous_summary: str,
    narrator: str,
) -> str:
    candidates = "、".join(sorted(_candidate_speakers(bible, narrator)))
    return (
        f"章节：{chapter.chapter_id} {chapter.title}\n\n"
        f"候选 speaker：{candidates}\n\n"
        f"Character Bible：\n{_bible_brief(bible)}\n\n"
        f"Chapter State：\n{_state_text(state)}\n\n"
        f"上一 chunk 摘要：\n{previous_summary or '无'}\n\n"
        f"chunk_id: {chunk.chunk_id}\n"
        f"source_start: {chunk.start}\n"
        f"source_end: {chunk.end}\n\n"
        f"context_before（只用于理解，不得输出）：\n{chunk.context_before}\n\n"
        f"target_text（只能输出这里面的原文）：\n{chunk.target_text}\n\n"
        f"context_after（只用于理解，不得输出）：\n{chunk.context_after}\n"
    )


def _clip_confidence(value: Any) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except Exception:
        return 0.5


def _normalize_pause(value: Any) -> int:
    try:
        return min(2000, max(0, int(value)))
    except Exception:
        return 500


def _normalize_speaker(raw: Any, bible: CharacterBible, candidates: set[str]) -> tuple[str, bool]:
    speaker = str(raw or "").strip() or "旁白"
    if speaker in candidates:
        return speaker, False
    if speaker in bible.aliases and bible.aliases[speaker] in candidates:
        return bible.aliases[speaker], True
    if "男" in speaker:
        return "未具名男声", True
    if "女" in speaker:
        return "未具名女声", True
    return "未知角色", True


def _validate_items(
    raw_items: list[dict[str, Any]],
    *,
    chapter: Chapter,
    chunk: TextChunk,
    bible: CharacterBible,
    candidates: set[str],
    start_id: int,
    confidence_threshold: float,
) -> list[PlanItem]:
    rows: list[PlanItem] = []
    cursor = 0
    next_id = start_id
    for raw in raw_items:
        text = str(raw.get("text", "")).strip()
        if not text:
            continue

        local_start = chunk.target_text.find(text, cursor)
        if local_start < 0:
            local_start = chunk.target_text.find(text)
        text_valid = local_start >= 0
        if text_valid:
            cursor = local_start + len(text)
            source_start = chunk.start + local_start
            source_end = source_start + len(text)
        else:
            source_start = -1
            source_end = -1

        speaker, normalized = _normalize_speaker(raw.get("speaker"), bible, candidates)
        kind = str(raw.get("kind", "narration"))
        if kind not in {"narration", "dialogue"}:
            kind = "dialogue"
        confidence = _clip_confidence(raw.get("confidence", 0.5))
        needs_review = bool(raw.get("needs_review", False)) or confidence < confidence_threshold
        needs_review = needs_review or not text_valid or normalized

        reason = str(raw.get("reason", "")).strip()
        if not text_valid:
            reason = (reason + "；" if reason else "") + "text 未在 target_text 中精确匹配"
        if normalized:
            reason = (reason + "；" if reason else "") + "speaker 已按候选集归一化"

        rows.append(
            PlanItem(
                id=next_id,
                chapter_id=chapter.chapter_id,
                chunk_id=chunk.chunk_id,
                source_start=source_start,
                source_end=source_end,
                speaker=speaker,
                text=text,
                kind=kind,  # type: ignore[arg-type]
                emotion=str(raw.get("emotion", "")).strip(),
                style_prompt=str(raw.get("style_prompt", "")).strip() or "按上下文自然朗读，保留情绪变化。",
                delivery=raw.get("delivery") if isinstance(raw.get("delivery"), dict) else {},
                pause_after_ms=_normalize_pause(raw.get("pause_after_ms")),
                confidence=confidence,
                reason=reason,
                needs_review=needs_review,
            )
        )
        next_id += 1
    return rows


def _fallback_item(chapter: Chapter, chunk: TextChunk, item_id: int, reason: str) -> PlanItem:
    return PlanItem(
        id=item_id,
        chapter_id=chapter.chapter_id,
        chunk_id=chunk.chunk_id,
        source_start=chunk.start,
        source_end=chunk.end,
        speaker="旁白",
        text=chunk.target_text.strip(),
        kind="narration",
        emotion="待复核",
        style_prompt="LLM 规划失败，按旁白临时朗读；本段必须人工复核。",
        pause_after_ms=800,
        confidence=0.2,
        reason=reason,
        needs_review=True,
    )


def _load_state(project_dir: Path, config: dict, chapter: Chapter) -> ChapterState | None:
    path = project_path(project_dir, config["bible"]["state_dir"]) / f"{chapter.chapter_id}.chapter_state.json"
    if not path.exists():
        return None
    return ChapterState.model_validate(read_json(path))


def run(project_dir: Path, config_path: Path | None = None, chapter: str | None = None) -> Path:
    config = load_config(project_dir, config_path)
    chapters = [Chapter.model_validate(row) for row in read_jsonl(project_path(project_dir, config["ingest"]["chapters_file"]))]
    selected = _selected_chapters(chapters, chapter)
    bible = CharacterBible.model_validate(read_json(project_path(project_dir, config["bible"]["output_file"])))
    planner_config = config["planner"]
    output_dir = project_path(project_dir, planner_config["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    ollama = config["ollama"]
    narrator = str(planner_config.get("narrator_name", "旁白"))
    confidence_threshold = float(planner_config.get("confidence_review_threshold", 0.7))
    last_output = output_dir

    for chapter_obj in selected:
        text = chapter_obj.text
        chunks = _build_chunks(
            text,
            target_chars=int(planner_config.get("target_chars", 1200)),
            overlap_chars=int(planner_config.get("overlap_chars", 500)),
            snap_limit=int(planner_config.get("snap_limit", 300)),
        )
        max_chunks = int(planner_config.get("max_chunks", 0))
        if max_chunks > 0:
            chunks = chunks[:max_chunks]

        state = _load_state(project_dir, config, chapter_obj)
        candidates = _candidate_speakers(bible, narrator)
        rows: list[PlanItem] = []
        previous_summary = ""
        next_id = 1
        logger.info("Planning chapter {} with {} chunks", chapter_obj.chapter_id, len(chunks))

        for chunk in tqdm(chunks, desc=f"planner {chapter_obj.chapter_id}"):
            try:
                data = ollama_generate_json(
                    url=ollama["url"],
                    model=ollama["model"],
                    system=SYSTEM_PROMPT,
                    prompt=_make_prompt(chapter_obj, chunk, bible, state, previous_summary, narrator),
                    schema=PLAN_SCHEMA,
                    temperature=float(ollama["temperature"]),
                    num_predict=int(ollama["num_predict"]),
                    keep_alive=str(ollama["keep_alive"]),
                    timeout_seconds=int(ollama["timeout_seconds"]),
                    num_ctx=int(ollama.get("num_ctx", 0)) or None,
                    task_name=f"planner.{chapter_obj.chapter_id}.{chunk.chunk_id}",
                    max_retries=0,
                )
                raw_items = data.get("items", []) if isinstance(data, dict) else []
                chunk_rows = _validate_items(
                    raw_items,
                    chapter=chapter_obj,
                    chunk=chunk,
                    bible=bible,
                    candidates=candidates,
                    start_id=next_id,
                    confidence_threshold=confidence_threshold,
                )
                if not chunk_rows:
                    chunk_rows = [_fallback_item(chapter_obj, chunk, next_id, "LLM 返回空 items")]
                previous_summary = str(data.get("summary", "")).strip() if isinstance(data, dict) else ""
            except Exception as exc:
                logger.warning("Planner failed for chapter {} chunk {}: {}", chapter_obj.chapter_id, chunk.chunk_id, exc)
                chunk_rows = [_fallback_item(chapter_obj, chunk, next_id, str(exc))]
                previous_summary = ""

            rows.extend(chunk_rows)
            next_id = rows[-1].id + 1

        output_path = output_dir / f"{chapter_obj.chapter_id}.plan.jsonl"
        write_jsonl(output_path, rows)
        logger.info("Wrote plan: {} ({} items)", output_path, len(rows))
        last_output = output_path
    return last_output

