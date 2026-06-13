from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger
from tqdm import tqdm

from novelreader_v2.common.config import load_config, project_path
from novelreader_v2.common.graph_schema import RetrievedContext
from novelreader_v2.common.jsonio import read_json, read_jsonl, write_jsonl
from novelreader_v2.common.ollama import ollama_generate_json
from novelreader_v2.common.schema import Chapter, PlanItem


@dataclass(frozen=True)
class SourceSpan:
    span_id: str
    local_start: int
    local_end: int
    text: str


PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "maxItems": 80,
            "items": {
                "type": "object",
                "properties": {
                    "span_ids": {"type": "array", "minItems": 1, "maxItems": 12, "items": {"type": "string"}},
                    "speaker": {"type": "string", "maxLength": 32},
                    "kind": {"type": "string", "enum": ["narration", "dialogue"]},
                    "emotion": {"type": "string", "maxLength": 80},
                    "intensity": {"type": "integer"},
                    "style_prompt": {"type": "string", "maxLength": 180},
                    "adapted_text": {"type": "string", "maxLength": 300},
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
                    "reason": {"type": "string", "maxLength": 180},
                    "evidence_ids": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
                    "needs_review": {"type": "boolean"},
                },
                "required": [
                    "span_ids",
                    "speaker",
                    "kind",
                    "emotion",
                    "intensity",
                    "style_prompt",
                    "pause_after_ms",
                    "confidence",
                    "reason",
                    "needs_review",
                ],
            },
        },
        "summary": {"type": "string", "maxLength": 240},
    },
    "required": ["items", "summary"],
}


SYSTEM_PROMPT = """你是中文小说有声书导演，只输出 JSON。

任务：根据 source_spans 和 retrieved_context 生成朗读计划。

关键规则：
- 不要复制原文 text。你只能输出 span_ids。
- source_spans 是 target_text 被代码确定性切出来的原文片段。
- 每个 span_id 必须且只能出现一次，不能遗漏、不能重复、不能新增不存在的 span_id。
- 每条 item 的 span_ids 必须保持原文顺序，并且必须是相邻连续片段。
- 可以把相邻 span 合并成一个朗读单元，但不能跨过中间 span。
- 如果某个 span 无法判断 speaker 或语气，也必须输出它，并设置 needs_review=true。
- speaker 优先使用 candidate_speakers 中的名字；不确定时用“未知角色”。
- kind 只能是 narration 或 dialogue。引号内对白、语气词、内心独白通常是 dialogue。
- style_prompt 必须具体，不要只写“自然中文对白/旁白”。
- evidence_ids 只能引用 retrieved_context.evidence 中出现的 id。
- adapted_text 可选，用于轻微朗读优化；不要改变剧情含义。
- summary 用一句话概括本 chunk 结束时上下文状态。
- 不要输出 Markdown，不要解释，只输出 JSON。"""


OPEN_TO_CLOSE = {"「": "」", "『": "』", "“": "”"}
TERMINAL_CHARS = set("。！？!?；;」』”")
SOFT_CHARS = set("，,、")


def _selected_chapters(chapters: list[Chapter], chapter: str | None) -> list[Chapter]:
    if not chapter:
        return chapters
    rows = [row for row in chapters if row.chapter_id == chapter]
    if not rows:
        raise ValueError(f"chapter {chapter!r} not found")
    return rows


def _context_dir(project_dir: Path, config: dict[str, Any], chapter_id: str) -> Path:
    return project_path(project_dir, config["context_retrieval"]["output_dir"]) / chapter_id


def _load_contexts(project_dir: Path, config: dict[str, Any], chapter_id: str) -> list[RetrievedContext]:
    context_dir = _context_dir(project_dir, config, chapter_id)
    paths = sorted(context_dir.glob("*.context.json"))
    if not paths:
        raise FileNotFoundError(f"context files not found: {context_dir}")
    return [RetrievedContext.model_validate(read_json(path)) for path in paths]


def _next_nonspace(text: str, pos: int) -> str:
    while pos < len(text) and text[pos].isspace():
        pos += 1
    return text[pos] if pos < len(text) else ""


def _trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _quote_end(text: str, start: int) -> int:
    close = OPEN_TO_CLOSE.get(text[start])
    if not close:
        return start + 1
    index = text.find(close, start + 1)
    if index < 0:
        return len(text)
    return index + 1


def _build_source_spans(text: str, max_chars: int = 180) -> list[SourceSpan]:
    spans: list[SourceSpan] = []
    index = 0
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break

        start = index
        if text[index] in OPEN_TO_CLOSE:
            end = _quote_end(text, index)
        else:
            last_soft = -1
            end = len(text)
            while index < len(text):
                char = text[index]
                if char in OPEN_TO_CLOSE and index > start:
                    end = index
                    break
                if char in SOFT_CHARS:
                    last_soft = index
                if char == "：" and _next_nonspace(text, index + 1) in OPEN_TO_CLOSE:
                    end = index + 1
                    break
                if char in TERMINAL_CHARS:
                    end = index + 1
                    break
                if index - start >= max_chars and last_soft > start:
                    end = last_soft + 1
                    break
                index += 1
            else:
                end = len(text)

        start, end = _trim_span(text, start, end)
        if end <= start:
            index += 1
            continue
        spans.append(SourceSpan(span_id=f"s{len(spans) + 1:03d}", local_start=start, local_end=end, text=text[start:end]))
        index = end
    return spans


def _name_map(context: RetrievedContext) -> dict[str, str]:
    rows: dict[str, str] = {}
    for character in context.characters:
        rows[character.id] = character.canonical_name
        rows[character.canonical_name] = character.canonical_name
        for alias in character.aliases:
            rows[alias] = character.canonical_name
    return rows


def _speaker_candidates(context: RetrievedContext) -> set[str]:
    rows = set(context.candidate_speakers)
    rows.update(["旁白", "未具名男声", "未具名女声", "未知角色"])
    for character in context.characters:
        rows.add(character.canonical_name)
    return rows


def _friendly_context(context: RetrievedContext) -> dict[str, Any]:
    names = _name_map(context)

    def short(text: Any, limit: int = 140) -> str:
        value = str(text or "").strip()
        return value[:limit]

    return {
        "candidate_speakers": context.candidate_speakers,
        "characters": [
            {
                "id": character.id,
                "name": character.canonical_name,
                "aliases": character.aliases[:4],
                "gender": character.gender,
                "role": short(character.role, 100),
                "personality": character.personality[:5],
                "speech_style": short(character.speech_style, 120),
                "voice_style": short(character.voice_style, 120),
                "confidence": character.confidence,
                "evidence_ids": character.evidence_ids[:6],
            }
            for character in context.characters
        ],
        "relations": [
            {
                "source": names.get(relation.source, relation.source),
                "target": names.get(relation.target, relation.target),
                "type": short(relation.type, 80),
                "attitude": short(relation.attitude, 100),
                "confidence": relation.confidence,
                "evidence_ids": relation.evidence_ids[:4],
            }
            for relation in context.relations
        ],
        "recent_events": [
            {
                "summary": short(event.summary, 120),
                "participants": [names.get(person, person) for person in event.participants],
                "impact": short(event.impact, 120),
                "confidence": event.confidence,
                "evidence_ids": event.evidence_ids[:4],
            }
            for event in context.recent_events
        ],
        "quote_evidence": [
            {
                "text": short(quote.text, 120),
                "speaker": names.get(quote.speaker, quote.speaker),
                "candidate_speakers": [names.get(person, person) for person in quote.candidate_speakers],
                "confidence": quote.confidence,
                "reason": short(quote.reason, 120),
                "evidence_ids": quote.evidence_ids[:4],
            }
            for quote in context.quote_evidence
        ],
        "evidence": [
            {
                "id": evidence.id,
                "text": short(evidence.text, 140),
                "start_char": evidence.start_char,
                "end_char": evidence.end_char,
            }
            for evidence in context.evidence
        ],
    }


def _make_prompt(
    chapter: Chapter,
    context: RetrievedContext,
    source_spans: list[SourceSpan],
    previous_summary: str,
    validation_errors: list[str] | None = None,
) -> str:
    span_rows = [{"id": span.span_id, "text": span.text} for span in source_spans]
    all_ids = [span.span_id for span in source_spans]
    retry_text = ""
    if validation_errors:
        retry_text = (
            "\n上一次输出无效，必须修正以下问题：\n"
            + "\n".join(f"- {error}" for error in validation_errors[:12])
            + "\n请重新输出完整 JSON，不要只输出修正片段。\n"
        )
    return (
        f"章节：{chapter.chapter_id} {chapter.title}\n"
        f"chunk_id: {context.chunk_id}\n"
        f"source_start: {context.source_start}\n"
        f"source_end: {context.source_end}\n\n"
        f"上一 chunk 的导演摘要：\n{previous_summary or context.previous_summary or '无'}\n\n"
        f"retrieved_context：\n{json.dumps(_friendly_context(context), ensure_ascii=False, separators=(',', ':'))}\n\n"
        f"context_before（只用于理解，不得输出 span）：\n{context.context_before}\n\n"
        f"source_spans（必须覆盖全部 id，且每个 id 只出现一次）：\n"
        f"{json.dumps(span_rows, ensure_ascii=False, separators=(',', ':'))}\n\n"
        f"all_span_ids：{json.dumps(all_ids, ensure_ascii=False)}\n\n"
        f"context_after（只用于理解，不得输出 span）：\n{context.context_after}\n"
        f"{retry_text}"
    )


def _clip_confidence(value: Any) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except Exception:
        return 0.5


def _clip_intensity(value: Any) -> int:
    try:
        return min(5, max(1, int(value)))
    except Exception:
        return 3


def _normalize_pause(value: Any) -> int:
    try:
        return min(2500, max(0, int(value)))
    except Exception:
        return 500


def _infer_focal_character(context: RetrievedContext) -> str:
    window_text = "\n".join([context.context_before, context.target_text, context.context_after])
    for character in context.characters:
        names = [character.canonical_name, *character.aliases]
        if any(name and name in window_text for name in names):
            return character.canonical_name
    if len(context.characters) == 1:
        return context.characters[0].canonical_name
    return ""


def _normalize_speaker(raw: Any, context: RetrievedContext, kind: str) -> tuple[str, bool]:
    speaker = str(raw or "").strip() or "未知角色"
    aliases = _name_map(context)
    candidates = _speaker_candidates(context)
    if speaker in candidates:
        return speaker, False
    if speaker in aliases and aliases[speaker] in candidates:
        return aliases[speaker], True
    if kind == "narration":
        return "旁白", False
    if any(token in speaker for token in ("主角", "主人公", "少年", "少女", "他", "她", "自己", "内心", "心声")):
        focal = _infer_focal_character(context)
        if focal:
            return focal, True
    if "旁白" in speaker or "叙述" in speaker:
        return "旁白", True
    if "男" in speaker:
        return "未具名男声", True
    if "女" in speaker:
        return "未具名女声", True
    return "未知角色", True


def _valid_evidence_ids(raw: Any, context: RetrievedContext) -> list[str]:
    allowed = {evidence.id for evidence in context.evidence}
    rows: list[str] = []
    if not isinstance(raw, list):
        return rows
    for value in raw:
        evidence_id = str(value).strip()
        if evidence_id in allowed and evidence_id not in rows:
            rows.append(evidence_id)
    return rows


def _raw_span_ids(raw: dict[str, Any]) -> list[str]:
    values = raw.get("span_ids", [])
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    rows: list[str] = []
    for value in values:
        span_id = str(value).strip()
        if span_id:
            rows.append(span_id)
    return rows


def _validate_span_coverage(raw_items: list[dict[str, Any]], source_spans: list[SourceSpan]) -> list[str]:
    allowed = [span.span_id for span in source_spans]
    allowed_set = set(allowed)
    seen: list[str] = []
    errors: list[str] = []
    index_by_id = {span_id: index for index, span_id in enumerate(allowed)}

    for item_index, raw in enumerate(raw_items, start=1):
        span_ids = _raw_span_ids(raw)
        if not span_ids:
            errors.append(f"item {item_index} 缺少 span_ids")
            continue
        invalid = [span_id for span_id in span_ids if span_id not in allowed_set]
        if invalid:
            errors.append(f"item {item_index} 包含不存在的 span_id: {invalid}")
            continue
        indices = [index_by_id[span_id] for span_id in span_ids]
        if indices != sorted(indices):
            errors.append(f"item {item_index} 的 span_ids 顺序错误: {span_ids}")
        if any(b != a + 1 for a, b in zip(indices, indices[1:])):
            errors.append(f"item {item_index} 的 span_ids 不连续: {span_ids}")
        seen.extend(span_ids)

    duplicated = sorted({span_id for span_id in seen if seen.count(span_id) > 1})
    missing = [span_id for span_id in allowed if span_id not in seen]
    if duplicated:
        errors.append(f"重复 span_id: {duplicated[:20]}")
    if missing:
        errors.append(f"遗漏 span_id: {missing[:30]}")
    return errors


def _rows_from_span_plan(
    raw_items: list[dict[str, Any]],
    *,
    chapter: Chapter,
    context: RetrievedContext,
    source_spans: list[SourceSpan],
    start_id: int,
    confidence_threshold: float,
) -> list[PlanItem]:
    span_by_id = {span.span_id: span for span in source_spans}
    rows: list[PlanItem] = []
    next_id = start_id
    for raw in raw_items:
        span_ids = _raw_span_ids(raw)
        spans = [span_by_id[span_id] for span_id in span_ids]
        local_start = spans[0].local_start
        local_end = spans[-1].local_end
        text = context.target_text[local_start:local_end].strip()
        kind = str(raw.get("kind", "narration"))
        if kind not in {"narration", "dialogue"}:
            kind = "dialogue"
        speaker, normalized = _normalize_speaker(raw.get("speaker"), context, kind)
        confidence = _clip_confidence(raw.get("confidence", 0.5))
        evidence_ids = _valid_evidence_ids(raw.get("evidence_ids", []), context)
        needs_review = bool(raw.get("needs_review", False)) or confidence < confidence_threshold or normalized
        reason = str(raw.get("reason", "")).strip()
        if normalized:
            reason = (reason + "；" if reason else "") + "speaker 已按候选集归一化"
        if not evidence_ids and speaker != "旁白":
            needs_review = True
            reason = (reason + "；" if reason else "") + "缺少可引用 evidence_ids"
        rows.append(
            PlanItem(
                id=next_id,
                chapter_id=chapter.chapter_id,
                chunk_id=context.chunk_id,
                source_start=context.source_start + local_start,
                source_end=context.source_start + local_end,
                span_ids=span_ids,
                speaker=speaker,
                text=text,
                kind=kind,  # type: ignore[arg-type]
                emotion=str(raw.get("emotion", "")).strip(),
                intensity=_clip_intensity(raw.get("intensity", 3)),
                style_prompt=str(raw.get("style_prompt", "")).strip(),
                adapted_text=str(raw.get("adapted_text", "")).strip(),
                delivery=raw.get("delivery") if isinstance(raw.get("delivery"), dict) else {},
                pause_after_ms=_normalize_pause(raw.get("pause_after_ms")),
                confidence=confidence,
                reason=reason,
                evidence_ids=evidence_ids,
                needs_review=needs_review,
            )
        )
        next_id += 1
    return rows


def _fallback_rows(chapter: Chapter, context: RetrievedContext, source_spans: list[SourceSpan], start_id: int, reason: str) -> list[PlanItem]:
    rows: list[PlanItem] = []
    next_id = start_id
    for span in source_spans:
        rows.append(
            PlanItem(
                id=next_id,
                chapter_id=chapter.chapter_id,
                chunk_id=context.chunk_id,
                source_start=context.source_start + span.local_start,
                source_end=context.source_start + span.local_end,
                span_ids=[span.span_id],
                speaker="旁白",
                text=span.text,
                kind="narration",
                emotion="待复核",
                intensity=2,
                style_prompt="Planner 规划失败，系统按 source span 自动补回，必须人工复核 speaker、语气和断句。",
                pause_after_ms=700,
                confidence=0.2,
                reason=reason,
                needs_review=True,
            )
        )
        next_id += 1
    return rows


def _plan_context(
    *,
    chapter: Chapter,
    context: RetrievedContext,
    source_spans: list[SourceSpan],
    previous_summary: str,
    config: dict[str, Any],
    planner_config: dict[str, Any],
    confidence_threshold: float,
    start_id: int,
) -> tuple[list[PlanItem], str]:
    ollama = config["ollama"]
    validation_errors: list[str] = []
    coverage_retries = int(planner_config.get("coverage_retries", 2))
    raw_summary = ""

    for attempt in range(coverage_retries + 1):
        data = ollama_generate_json(
            url=ollama["url"],
            model=ollama["model"],
            system=SYSTEM_PROMPT,
            prompt=_make_prompt(chapter, context, source_spans, previous_summary, validation_errors),
            schema=PLAN_SCHEMA,
            temperature=float(ollama["temperature"]),
            num_predict=int(planner_config.get("num_predict", ollama["num_predict"])),
            keep_alive=str(ollama["keep_alive"]),
            timeout_seconds=int(ollama["timeout_seconds"]),
            num_ctx=int(planner_config.get("num_ctx", ollama.get("num_ctx", 0))) or None,
            task_name=f"planner.{chapter.chapter_id}.{context.chunk_id}.try{attempt + 1}",
            max_retries=int(planner_config.get("max_retries", 1)),
        )
        raw_items = data.get("items", []) if isinstance(data, dict) else []
        raw_summary = str(data.get("summary", "")).strip() if isinstance(data, dict) else ""
        validation_errors = _validate_span_coverage(raw_items, source_spans)
        if not validation_errors:
            return (
                _rows_from_span_plan(
                    raw_items,
                    chapter=chapter,
                    context=context,
                    source_spans=source_spans,
                    start_id=start_id,
                    confidence_threshold=confidence_threshold,
                ),
                raw_summary,
            )
        logger.warning(
            "Planner span coverage invalid chapter {} chunk {} attempt {}: {}",
            chapter.chapter_id,
            context.chunk_id,
            attempt + 1,
            "; ".join(validation_errors[:4]),
        )

    reason = "span coverage failed after retries: " + "; ".join(validation_errors[:8])
    return _fallback_rows(chapter, context, source_spans, start_id, reason), raw_summary


def run(project_dir: Path, config_path: Path | None = None, chapter: str | None = None) -> Path:
    config = load_config(project_dir, config_path)
    chapters = [Chapter.model_validate(row) for row in read_jsonl(project_path(project_dir, config["ingest"]["chapters_file"]))]
    selected = _selected_chapters(chapters, chapter)
    planner_config = config["planner"]
    output_dir = project_path(project_dir, planner_config["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    confidence_threshold = float(planner_config.get("confidence_review_threshold", 0.7))

    last_output = output_dir
    for chapter_obj in selected:
        contexts = _load_contexts(project_dir, config, chapter_obj.chapter_id)
        rows: list[PlanItem] = []
        previous_summary = ""
        next_id = 1
        logger.info("Planning chapter {} with {} retrieved contexts", chapter_obj.chapter_id, len(contexts))

        for context in tqdm(contexts, desc=f"planner {chapter_obj.chapter_id}"):
            source_spans = _build_source_spans(context.target_text, int(planner_config.get("source_span_max_chars", 180)))
            try:
                chunk_rows, previous_summary = _plan_context(
                    chapter=chapter_obj,
                    context=context,
                    source_spans=source_spans,
                    previous_summary=previous_summary,
                    config=config,
                    planner_config=planner_config,
                    confidence_threshold=confidence_threshold,
                    start_id=next_id,
                )
            except Exception as exc:
                logger.warning("Planner failed for chapter {} chunk {}: {}", chapter_obj.chapter_id, context.chunk_id, exc)
                chunk_rows = _fallback_rows(chapter_obj, context, source_spans, next_id, str(exc))
                previous_summary = ""

            rows.extend(chunk_rows)
            next_id = rows[-1].id + 1

        output_path = output_dir / f"{chapter_obj.chapter_id}.plan.jsonl"
        write_jsonl(output_path, rows)
        logger.info("Wrote plan: {} ({} items)", output_path, len(rows))
        last_output = output_path
    return last_output

