from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from loguru import logger
from tqdm import tqdm

from novelreader_v2.common.chunking import build_chunks, selected_chapters
from novelreader_v2.common.config import load_config, project_path
from novelreader_v2.common.graph_schema import (
    BookGraph,
    ChapterGraph,
    CharacterNode,
    Evidence,
    QuoteEvidence,
    RelationEdge,
    StoryEvent,
)
from novelreader_v2.common.jsonio import read_json, read_jsonl, write_json
from novelreader_v2.common.ollama import ollama_generate_json
from novelreader_v2.common.schema import Chapter


GRAPH_SCHEMA = {
    "type": "object",
    "properties": {
        "characters": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "canonical_name": {"type": "string", "maxLength": 24},
                    "aliases": {"type": "array", "maxItems": 6, "items": {"type": "string", "maxLength": 24}},
                    "gender": {"type": "string", "maxLength": 12},
                    "role": {"type": "string", "maxLength": 80},
                    "personality": {"type": "array", "maxItems": 5, "items": {"type": "string", "maxLength": 32}},
                    "speech_style": {"type": "string", "maxLength": 120},
                    "voice_style": {"type": "string", "maxLength": 120},
                    "confidence": {"type": "number"},
                    "evidence": {"type": "array", "maxItems": 3, "items": {"type": "string", "maxLength": 160}},
                },
                "required": ["canonical_name", "aliases", "confidence", "evidence"],
            },
        },
        "relations": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "maxLength": 24},
                    "target": {"type": "string", "maxLength": 24},
                    "type": {"type": "string", "maxLength": 60},
                    "attitude": {"type": "string", "maxLength": 100},
                    "confidence": {"type": "number"},
                    "evidence": {"type": "array", "maxItems": 2, "items": {"type": "string", "maxLength": 160}},
                },
                "required": ["source", "target", "type", "confidence", "evidence"],
            },
        },
        "events": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "maxLength": 120},
                    "participants": {"type": "array", "maxItems": 5, "items": {"type": "string", "maxLength": 24}},
                    "impact": {"type": "string", "maxLength": 120},
                    "confidence": {"type": "number"},
                    "evidence": {"type": "array", "maxItems": 2, "items": {"type": "string", "maxLength": 160}},
                },
                "required": ["summary", "participants", "confidence", "evidence"],
            },
        },
        "quotes": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "maxLength": 160},
                    "candidate_speakers": {"type": "array", "maxItems": 4, "items": {"type": "string", "maxLength": 24}},
                    "speaker": {"type": "string", "maxLength": 24},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string", "maxLength": 160},
                    "evidence": {"type": "array", "maxItems": 2, "items": {"type": "string", "maxLength": 160}},
                },
                "required": ["text", "candidate_speakers", "confidence", "reason", "evidence"],
            },
        },
        "chapter_summary": {"type": "string", "maxLength": 240},
        "unresolved": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 120}},
    },
    "required": ["characters", "relations", "events", "quotes", "chapter_summary", "unresolved"],
}


SYSTEM_PROMPT = """你是中文小说知识图谱抽取器，只输出 JSON。

任务：从当前章节窗口中抽取证据化的故事图谱，用于后续判断“谁在说话”和“这句话应该怎么读”。

必须抽取：
- characters：真实人物或明确临时角色。不要把旁白、声音、房间、时间、身体部位、抽象称呼当人物。
- relations：人物之间的关系、态度、压制、冲突、亲密、师徒、敌对等。
- events：影响后续语气和人物状态的重要事件。
- quotes：有引号、语气词、内心独白、喊叫、低语、沉默等可能影响 speaker 判断的文本。

硬性要求：
- 只抽关键项，不要穷举。characters 最多 8 条，relations 最多 10 条，events 最多 8 条，quotes 最多 12 条。
- 每条 evidence 最多摘 1 到 2 句短原文，不要复制整段。
- 人名保持中文原文，不要拼音化，不要翻译成英文。
- 每条结论必须给 evidence，evidence 必须尽量摘自原文，不要自己编。
- 不确定就降低 confidence，并写入 unresolved。
- quotes 的 speaker 不确定时可以留空，但 candidate_speakers 要列出可能人物。
- 不要输出 Markdown，不要解释，只输出 JSON。"""


STOP_NAMES = {
    "旁白",
    "声音",
    "房间",
    "时候",
    "自己",
    "身体",
    "少女",
    "少年",
    "男子",
    "女子",
    "男人",
    "女人",
    "主人",
    "众人",
}


def _stable_id(prefix: str, *parts: str) -> str:
    raw = "\n".join(parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha1(raw).hexdigest()[:12]}"


def _clip_confidence(value: Any) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except Exception:
        return 0.0


def _clean_text(value: Any, limit: int = 400) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit].strip()


def _clean_list(values: Any, limit: int = 12) -> list[str]:
    rows: list[str] = []
    if not isinstance(values, list):
        return rows
    for value in values:
        text = _clean_text(value, 120)
        if text and text not in rows:
            rows.append(text)
        if len(rows) >= limit:
            break
    return rows


def _valid_character_name(name: str) -> bool:
    name = _clean_text(name, 30)
    if not (2 <= len(name) <= 16):
        return False
    if name in STOP_NAMES:
        return False
    return bool(re.search(r"[\u4e00-\u9fff]", name))


def _find_span(chapter_text: str, evidence_text: str) -> tuple[int, int]:
    if not evidence_text:
        return -1, -1
    index = chapter_text.find(evidence_text)
    if index >= 0:
        return index, index + len(evidence_text)

    compact = re.sub(r"\s+", "", evidence_text)
    if compact and compact != evidence_text:
        index = chapter_text.find(compact)
        if index >= 0:
            return index, index + len(compact)
    return -1, -1


def _evidence_ids(
    *,
    chapter: Chapter,
    raw_evidence: Any,
    chapter_graph: ChapterGraph,
    book_graph: BookGraph,
    source: str,
) -> list[str]:
    ids: list[str] = []
    for text in _clean_list(raw_evidence, limit=8):
        evidence_id = _stable_id("ev", chapter.chapter_id, text)
        start, end = _find_span(chapter.text, text)
        evidence = Evidence(
            id=evidence_id,
            chapter_id=chapter.chapter_id,
            text=text,
            start_char=start,
            end_char=end,
            source=source,
        )
        chapter_graph.evidence[evidence_id] = evidence
        book_graph.evidence[evidence_id] = evidence
        if evidence_id not in ids:
            ids.append(evidence_id)
    return ids


def _load_book_graph(path: Path) -> BookGraph:
    if path.exists():
        return BookGraph.model_validate(read_json(path))
    return BookGraph()


def _resolve_character_id(name: str, aliases: list[str], book_graph: BookGraph) -> str:
    candidates = [_clean_text(name, 30), *aliases]
    for candidate in candidates:
        if candidate in book_graph.aliases:
            return book_graph.aliases[candidate]
    for character_id, node in book_graph.characters.items():
        known = {node.canonical_name, *node.aliases}
        if any(candidate in known for candidate in candidates):
            return character_id
    return _stable_id("char", candidates[0])


def _merge_values(old: list[str], new: list[str], limit: int = 24) -> list[str]:
    rows: list[str] = []
    for value in [*old, *new]:
        value = _clean_text(value, 160)
        if value and value not in rows:
            rows.append(value)
        if len(rows) >= limit:
            break
    return rows


def _merge_character(existing: CharacterNode | None, incoming: CharacterNode) -> CharacterNode:
    if not existing:
        return incoming
    return CharacterNode(
        id=existing.id,
        canonical_name=existing.canonical_name or incoming.canonical_name,
        aliases=_merge_values(existing.aliases, incoming.aliases, 24),
        gender=incoming.gender if incoming.gender and incoming.gender != "未知" else existing.gender,
        role=incoming.role or existing.role,
        personality=_merge_values(existing.personality, incoming.personality, 24),
        speech_style=incoming.speech_style or existing.speech_style,
        voice_style=incoming.voice_style or existing.voice_style,
        confidence=max(existing.confidence, incoming.confidence),
        evidence_ids=_merge_values(existing.evidence_ids, incoming.evidence_ids, 40),
    )


def _ensure_character(
    *,
    raw_name: str,
    aliases: list[str],
    chapter_graph: ChapterGraph,
    book_graph: BookGraph,
) -> str | None:
    name = _clean_text(raw_name, 30)
    if not _valid_character_name(name):
        return None
    character_id = _resolve_character_id(name, aliases, book_graph)
    if character_id not in book_graph.characters:
        node = CharacterNode(id=character_id, canonical_name=name, aliases=aliases)
        book_graph.characters[character_id] = node
    chapter_graph.characters.setdefault(character_id, book_graph.characters[character_id])
    book_graph.aliases[name] = character_id
    for alias in aliases:
        if _valid_character_name(alias):
            book_graph.aliases[alias] = character_id
    return character_id


def _ingest_characters(data: dict[str, Any], chapter: Chapter, chapter_graph: ChapterGraph, book_graph: BookGraph) -> None:
    for raw in data.get("characters", []):
        if not isinstance(raw, dict):
            continue
        name = _clean_text(raw.get("canonical_name"), 30)
        aliases = _clean_list(raw.get("aliases"), 12)
        if not _valid_character_name(name):
            continue
        evidence_ids = _evidence_ids(
            chapter=chapter,
            raw_evidence=raw.get("evidence", []),
            chapter_graph=chapter_graph,
            book_graph=book_graph,
            source="character",
        )
        character_id = _resolve_character_id(name, aliases, book_graph)
        incoming = CharacterNode(
            id=character_id,
            canonical_name=name,
            aliases=aliases,
            gender=_clean_text(raw.get("gender"), 20) or "未知",
            role=_clean_text(raw.get("role"), 160),
            personality=_clean_list(raw.get("personality"), 10),
            speech_style=_clean_text(raw.get("speech_style"), 240),
            voice_style=_clean_text(raw.get("voice_style"), 240),
            confidence=_clip_confidence(raw.get("confidence")),
            evidence_ids=evidence_ids,
        )
        merged = _merge_character(book_graph.characters.get(character_id), incoming)
        book_graph.characters[character_id] = merged
        chapter_graph.characters[character_id] = merged
        book_graph.aliases[name] = character_id
        for alias in aliases:
            if _valid_character_name(alias):
                book_graph.aliases[alias] = character_id


def _resolve_name(raw_name: Any, chapter_graph: ChapterGraph, book_graph: BookGraph) -> str | None:
    name = _clean_text(raw_name, 30)
    if not name:
        return None
    if name in book_graph.aliases:
        return book_graph.aliases[name]
    if _valid_character_name(name):
        return _ensure_character(raw_name=name, aliases=[], chapter_graph=chapter_graph, book_graph=book_graph)
    return None


def _append_unique_by_id(rows: list[Any], item: Any) -> None:
    if not any(getattr(row, "id", None) == item.id for row in rows):
        rows.append(item)


def _ingest_relations(data: dict[str, Any], chapter: Chapter, chapter_graph: ChapterGraph, book_graph: BookGraph) -> None:
    for raw in data.get("relations", []):
        if not isinstance(raw, dict):
            continue
        source = _resolve_name(raw.get("source"), chapter_graph, book_graph)
        target = _resolve_name(raw.get("target"), chapter_graph, book_graph)
        if not source or not target or source == target:
            continue
        relation_type = _clean_text(raw.get("type"), 120)
        attitude = _clean_text(raw.get("attitude"), 180)
        evidence_ids = _evidence_ids(
            chapter=chapter,
            raw_evidence=raw.get("evidence", []),
            chapter_graph=chapter_graph,
            book_graph=book_graph,
            source="relation",
        )
        relation = RelationEdge(
            id=_stable_id("rel", source, target, relation_type, attitude, *evidence_ids),
            source=source,
            target=target,
            type=relation_type,
            attitude=attitude,
            confidence=_clip_confidence(raw.get("confidence")),
            evidence_ids=evidence_ids,
        )
        _append_unique_by_id(chapter_graph.relations, relation)
        _append_unique_by_id(book_graph.relations, relation)


def _ingest_events(data: dict[str, Any], chapter: Chapter, chapter_graph: ChapterGraph, book_graph: BookGraph) -> None:
    for raw in data.get("events", []):
        if not isinstance(raw, dict):
            continue
        summary = _clean_text(raw.get("summary"), 220)
        if not summary:
            continue
        participants = [
            character_id
            for value in raw.get("participants", [])
            if (character_id := _resolve_name(value, chapter_graph, book_graph))
        ]
        evidence_ids = _evidence_ids(
            chapter=chapter,
            raw_evidence=raw.get("evidence", []),
            chapter_graph=chapter_graph,
            book_graph=book_graph,
            source="event",
        )
        event = StoryEvent(
            id=_stable_id("event", chapter.chapter_id, summary, *evidence_ids),
            chapter_id=chapter.chapter_id,
            summary=summary,
            participants=_merge_values([], participants, 12),
            impact=_clean_text(raw.get("impact"), 220),
            confidence=_clip_confidence(raw.get("confidence")),
            evidence_ids=evidence_ids,
        )
        _append_unique_by_id(chapter_graph.events, event)
        _append_unique_by_id(book_graph.events, event)


def _ingest_quotes(data: dict[str, Any], chapter: Chapter, chapter_graph: ChapterGraph, book_graph: BookGraph) -> None:
    for raw in data.get("quotes", []):
        if not isinstance(raw, dict):
            continue
        text = _clean_text(raw.get("text"), 260)
        if not text:
            continue
        speaker = _resolve_name(raw.get("speaker"), chapter_graph, book_graph) or ""
        candidates = [
            character_id
            for value in raw.get("candidate_speakers", [])
            if (character_id := _resolve_name(value, chapter_graph, book_graph))
        ]
        evidence_ids = _evidence_ids(
            chapter=chapter,
            raw_evidence=raw.get("evidence", []),
            chapter_graph=chapter_graph,
            book_graph=book_graph,
            source="quote",
        )
        quote = QuoteEvidence(
            id=_stable_id("quote", chapter.chapter_id, text, speaker, *evidence_ids),
            chapter_id=chapter.chapter_id,
            text=text,
            candidate_speakers=_merge_values([], candidates, 8),
            speaker=speaker,
            confidence=_clip_confidence(raw.get("confidence")),
            reason=_clean_text(raw.get("reason"), 260),
            evidence_ids=evidence_ids,
        )
        _append_unique_by_id(chapter_graph.quotes, quote)
        _append_unique_by_id(book_graph.quotes, quote)


def _graph_brief(book_graph: BookGraph, limit: int = 12) -> str:
    rows = []
    for node in list(book_graph.characters.values())[:limit]:
        rows.append(
            {
                "id": node.id,
                "name": node.canonical_name,
                "aliases": node.aliases[:5],
                "gender": node.gender,
                "role": node.role,
                "personality": node.personality[:5],
                "speech_style": node.speech_style,
            }
        )
    return json.dumps({"characters": rows}, ensure_ascii=False)


def _make_prompt(chapter: Chapter, chunk_id: int, target_text: str, book_graph: BookGraph) -> str:
    return (
        f"已有全书图谱摘要：\n{_graph_brief(book_graph)}\n\n"
        f"章节：{chapter.chapter_id} {chapter.title}\n"
        f"窗口：{chunk_id}\n\n"
        f"当前章节窗口文本：\n{target_text}\n"
    )


def _process_response(data: dict[str, Any], chapter: Chapter, chapter_graph: ChapterGraph, book_graph: BookGraph) -> None:
    _ingest_characters(data, chapter, chapter_graph, book_graph)
    _ingest_relations(data, chapter, chapter_graph, book_graph)
    _ingest_events(data, chapter, chapter_graph, book_graph)
    _ingest_quotes(data, chapter, chapter_graph, book_graph)
    summary = _clean_text(data.get("chapter_summary"), 600)
    if summary:
        chapter_graph.summary = summary
        book_graph.chapter_summaries[chapter.chapter_id] = summary
    for item in _clean_list(data.get("unresolved"), 30):
        if item not in chapter_graph.unresolved:
            chapter_graph.unresolved.append(item)
        if item not in book_graph.unresolved:
            book_graph.unresolved.append(item)


def run(project_dir: Path, config_path: Path | None = None, chapter: str | None = None) -> Path:
    config = load_config(project_dir, config_path)
    graph_config = config["graph_index"]
    graph_dir = project_path(project_dir, graph_config["output_dir"])
    chapter_dir = graph_dir / "chapters"
    chapter_dir.mkdir(parents=True, exist_ok=True)
    book_graph_path = project_path(project_dir, graph_config["book_graph_file"])

    chapters = [Chapter.model_validate(row) for row in read_jsonl(project_path(project_dir, config["ingest"]["chapters_file"]))]
    selected = selected_chapters(chapters, chapter)
    book_graph = _load_book_graph(book_graph_path)
    ollama = config["ollama"]

    last_output = book_graph_path
    for chapter_obj in selected:
        logger.info("Indexing graph for chapter {} {}", chapter_obj.chapter_id, chapter_obj.title)
        chapter_graph = ChapterGraph(chapter_id=chapter_obj.chapter_id, title=chapter_obj.title)
        chunks = build_chunks(
            chapter_obj.text,
            target_chars=int(graph_config.get("window_chars", 5000)),
            overlap_chars=int(graph_config.get("overlap_chars", 500)),
            snap_limit=int(graph_config.get("snap_limit", 300)),
        )
        max_windows = int(graph_config.get("max_windows", 0))
        if max_windows > 0:
            chunks = chunks[:max_windows]

        for chunk in tqdm(chunks, desc=f"graph {chapter_obj.chapter_id}"):
            try:
                data = ollama_generate_json(
                    url=ollama["url"],
                    model=ollama["model"],
                    system=SYSTEM_PROMPT,
                    prompt=_make_prompt(chapter_obj, chunk.chunk_id, chunk.target_text, book_graph),
                    schema=GRAPH_SCHEMA,
                    temperature=float(ollama["temperature"]),
                    num_predict=int(graph_config.get("num_predict", ollama["num_predict"])),
                    keep_alive=str(ollama["keep_alive"]),
                    timeout_seconds=int(ollama["timeout_seconds"]),
                    num_ctx=int(ollama.get("num_ctx", 0)) or None,
                    task_name=f"graph_index.{chapter_obj.chapter_id}.{chunk.chunk_id}",
                    max_retries=int(graph_config.get("max_retries", 1)),
                )
                if not isinstance(data, dict):
                    raise TypeError("graph_index response is not an object")
                _process_response(data, chapter_obj, chapter_graph, book_graph)
            except Exception as exc:
                logger.warning("Graph indexing failed for chapter {} window {}: {}", chapter_obj.chapter_id, chunk.chunk_id, exc)
                message = f"window {chunk.chunk_id}: {exc}"
                if message not in chapter_graph.unresolved:
                    chapter_graph.unresolved.append(message)
                if message not in book_graph.unresolved:
                    book_graph.unresolved.append(message)

        chapter_path = chapter_dir / f"{chapter_obj.chapter_id}.graph.json"
        write_json(chapter_path, chapter_graph)
        write_json(book_graph_path, book_graph)
        logger.info(
            "Wrote graph: {} ({} characters, {} quotes)",
            chapter_path,
            len(chapter_graph.characters),
            len(chapter_graph.quotes),
        )
        last_output = chapter_path

    logger.info("Wrote book graph: {} ({} characters)", book_graph_path, len(book_graph.characters))
    return last_output
