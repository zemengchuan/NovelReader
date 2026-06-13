from __future__ import annotations

import json
from pathlib import Path

from loguru import logger
from tqdm import tqdm

from novelreader_v2.common.chunking import build_chunks, selected_chapters
from novelreader_v2.common.config import load_config, project_path
from novelreader_v2.common.graph_schema import (
    BookGraph,
    CharacterNode,
    Evidence,
    QuoteEvidence,
    RelationEdge,
    RetrievedContext,
    StoryEvent,
)
from novelreader_v2.common.jsonio import read_json, read_jsonl, write_json
from novelreader_v2.common.schema import Chapter


UNKNOWN_SPEAKERS = ["旁白", "未具名男声", "未具名女声", "未知角色"]


def _load_book_graph(path: Path) -> BookGraph:
    if not path.exists():
        raise FileNotFoundError(f"book graph not found: {path}")
    return BookGraph.model_validate(read_json(path))


def _contains_mention(text: str, character: CharacterNode) -> bool:
    names = [character.canonical_name, *character.aliases]
    return any(name and name in text for name in names)


def _overlaps(evidence: Evidence | None, start: int, end: int, margin: int) -> bool:
    if evidence is None or evidence.start_char < 0 or evidence.end_char < 0:
        return False
    return evidence.end_char >= start - margin and evidence.start_char <= end + margin


def _evidence_overlaps(evidence_ids: list[str], graph: BookGraph, start: int, end: int, margin: int) -> bool:
    return any(_overlaps(graph.evidence.get(evidence_id), start, end, margin) for evidence_id in evidence_ids)


def _select_character_ids(graph: BookGraph, window_text: str, start: int, end: int, margin: int, limit: int) -> list[str]:
    selected: list[str] = []
    for character_id, character in graph.characters.items():
        if _contains_mention(window_text, character) or _evidence_overlaps(character.evidence_ids, graph, start, end, margin):
            selected.append(character_id)

    # Expand one hop through relations so the model sees who the mentioned person is interacting with.
    expanded = list(selected)
    for relation in graph.relations:
        if relation.source in selected and relation.target not in expanded:
            expanded.append(relation.target)
        if relation.target in selected and relation.source not in expanded:
            expanded.append(relation.source)

    if not expanded:
        # Fallback: keep a small set of known characters instead of giving the planner an empty world.
        expanded = list(graph.characters.keys())[:limit]
    return expanded[:limit]


def _select_relations(graph: BookGraph, character_ids: set[str], limit: int) -> list[RelationEdge]:
    rows = [
        relation
        for relation in graph.relations
        if relation.source in character_ids or relation.target in character_ids
    ]
    rows.sort(key=lambda row: row.confidence, reverse=True)
    return rows[:limit]


def _select_events(graph: BookGraph, character_ids: set[str], start: int, end: int, margin: int, limit: int) -> list[StoryEvent]:
    rows = []
    for event in graph.events:
        if any(character_id in character_ids for character_id in event.participants) or _evidence_overlaps(event.evidence_ids, graph, start, end, margin):
            rows.append(event)
    rows.sort(
        key=lambda row: max((graph.evidence.get(evidence_id).start_char for evidence_id in row.evidence_ids if evidence_id in graph.evidence), default=-1),
        reverse=True,
    )
    return rows[:limit]


def _select_quotes(
    graph: BookGraph,
    character_ids: set[str],
    window_text: str,
    start: int,
    end: int,
    margin: int,
    limit: int,
) -> list[QuoteEvidence]:
    rows = []
    for quote in graph.quotes:
        speaker_match = quote.speaker in character_ids or any(candidate in character_ids for candidate in quote.candidate_speakers)
        text_match = quote.text and quote.text in window_text
        evidence_match = _evidence_overlaps(quote.evidence_ids, graph, start, end, margin)
        if speaker_match or text_match or evidence_match:
            rows.append(quote)
    rows.sort(key=lambda row: row.confidence, reverse=True)
    return rows[:limit]


def _collect_evidence(
    graph: BookGraph,
    characters: list[CharacterNode],
    relations: list[RelationEdge],
    events: list[StoryEvent],
    quotes: list[QuoteEvidence],
    limit: int,
) -> list[Evidence]:
    ids: list[str] = []
    for obj in [*characters, *relations, *events, *quotes]:
        for evidence_id in obj.evidence_ids:
            if evidence_id not in ids and evidence_id in graph.evidence:
                ids.append(evidence_id)
            if len(ids) >= limit:
                break
        if len(ids) >= limit:
            break
    rows = [graph.evidence[evidence_id] for evidence_id in ids]
    rows.sort(key=lambda row: (row.chapter_id, row.start_char if row.start_char >= 0 else 10**12))
    return rows


def _candidate_names(characters: list[CharacterNode], narrator: str) -> list[str]:
    rows = [narrator, "未具名男声", "未具名女声", "未知角色"]
    for character in characters:
        if character.canonical_name not in rows:
            rows.append(character.canonical_name)
    return rows


def _write_manifest(output_dir: Path, contexts: list[RetrievedContext]) -> None:
    write_json(
        output_dir / "manifest.json",
        {
            "chapter_id": contexts[0].chapter_id if contexts else "",
            "chunks": [
                {
                    "chunk_id": context.chunk_id,
                    "source_start": context.source_start,
                    "source_end": context.source_end,
                    "candidate_speakers": context.candidate_speakers,
                }
                for context in contexts
            ],
        },
    )


def run(project_dir: Path, config_path: Path | None = None, chapter: str | None = None) -> Path:
    config = load_config(project_dir, config_path)
    graph = _load_book_graph(project_path(project_dir, config["graph_index"]["book_graph_file"]))
    context_config = config["context_retrieval"]
    planner_config = config["planner"]
    chapters = [Chapter.model_validate(row) for row in read_jsonl(project_path(project_dir, config["ingest"]["chapters_file"]))]
    selected = selected_chapters(chapters, chapter)
    output_root = project_path(project_dir, context_config["output_dir"])
    output_root.mkdir(parents=True, exist_ok=True)

    last_output = output_root
    for chapter_obj in selected:
        chunks = build_chunks(
            chapter_obj.text,
            target_chars=int(planner_config.get("target_chars", 1200)),
            overlap_chars=int(planner_config.get("overlap_chars", 500)),
            snap_limit=int(planner_config.get("snap_limit", 300)),
        )
        max_chunks = int(planner_config.get("max_chunks", 0))
        if max_chunks > 0:
            chunks = chunks[:max_chunks]

        output_dir = output_root / chapter_obj.chapter_id
        output_dir.mkdir(parents=True, exist_ok=True)
        contexts: list[RetrievedContext] = []
        margin = int(context_config.get("evidence_margin_chars", 600))
        previous_summary = ""

        logger.info("Retrieving context for chapter {} with {} chunks", chapter_obj.chapter_id, len(chunks))
        for chunk in tqdm(chunks, desc=f"context {chapter_obj.chapter_id}"):
            window_text = "\n".join([chunk.context_before, chunk.target_text, chunk.context_after])
            character_ids = _select_character_ids(
                graph,
                window_text,
                chunk.start,
                chunk.end,
                margin,
                int(context_config.get("max_characters", 8)),
            )
            character_set = set(character_ids)
            characters = [graph.characters[character_id] for character_id in character_ids if character_id in graph.characters]
            relations = _select_relations(graph, character_set, int(context_config.get("max_relations", 12)))
            events = _select_events(graph, character_set, chunk.start, chunk.end, margin, int(context_config.get("max_events", 10)))
            quotes = _select_quotes(graph, character_set, window_text, chunk.start, chunk.end, margin, int(context_config.get("max_quotes", 12)))
            evidence = _collect_evidence(
                graph,
                characters,
                relations,
                events,
                quotes,
                int(context_config.get("max_evidence", 24)),
            )
            context = RetrievedContext(
                chapter_id=chapter_obj.chapter_id,
                chunk_id=chunk.chunk_id,
                source_start=chunk.start,
                source_end=chunk.end,
                context_before=chunk.context_before,
                target_text=chunk.target_text,
                context_after=chunk.context_after,
                candidate_speakers=_candidate_names(characters, str(planner_config.get("narrator_name", "旁白"))),
                characters=characters,
                relations=relations,
                recent_events=events,
                quote_evidence=quotes,
                evidence=evidence,
                previous_summary=previous_summary,
            )
            write_json(output_dir / f"{chunk.chunk_id:04d}.context.json", context)
            contexts.append(context)
            previous_summary = graph.chapter_summaries.get(chapter_obj.chapter_id, previous_summary)

        _write_manifest(output_dir, contexts)
        logger.info("Wrote contexts: {} ({} chunks)", output_dir, len(contexts))
        last_output = output_dir

    logger.debug("Context retrieval config: {}", json.dumps(context_config, ensure_ascii=False))
    return last_output

