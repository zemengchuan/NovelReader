from __future__ import annotations

from dataclasses import dataclass

from novelreader_v2.common.schema import Chapter


@dataclass(frozen=True)
class TextChunk:
    chunk_id: int
    start: int
    end: int
    context_before: str
    target_text: str
    context_after: str


def selected_chapters(chapters: list[Chapter], chapter: str | None) -> list[Chapter]:
    if not chapter:
        return chapters
    rows = [row for row in chapters if row.chapter_id == chapter]
    if not rows:
        raise ValueError(f"chapter {chapter!r} not found")
    return rows


def snap_forward(text: str, position: int, limit: int) -> int:
    if position >= len(text):
        return len(text)
    stop = min(len(text), position + limit)
    for index in range(position, stop):
        if text[index] in "\n。！？；」”":
            return index + 1
    return position


def build_chunks(text: str, target_chars: int, overlap_chars: int, snap_limit: int) -> list[TextChunk]:
    chunks: list[TextChunk] = []
    start = 0
    chunk_id = 1
    while start < len(text):
        rough_end = min(len(text), start + target_chars)
        end = snap_forward(text, rough_end, snap_limit) if rough_end < len(text) else len(text)
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

