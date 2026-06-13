from __future__ import annotations

import re
from pathlib import Path

from loguru import logger

from novelreader_v2.common.config import load_config, project_path
from novelreader_v2.common.jsonio import read_text, write_jsonl
from novelreader_v2.common.schema import Chapter


CHAPTER_RE = re.compile(
    r"^\s*((?:第[一二三四五六七八九十百千万零〇\d]+[章节回卷].*)|(?:[一二三四五六七八九十百千万零〇\d]+[、.．].*))\s*$"
)


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\ufeff", "")
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _chapter_id(index: int) -> str:
    return f"{index:03d}"


def split_chapters(text: str) -> list[Chapter]:
    lines = text.splitlines()
    chapters: list[Chapter] = []
    current_title = ""
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_title, current_lines
        body = "\n".join(current_lines).strip()
        if not current_title and not body:
            return
        chapters.append(
            Chapter(
                chapter_id=_chapter_id(len(chapters) + 1),
                title=current_title or f"第{len(chapters) + 1}章",
                text=body,
            )
        )
        current_lines = []

    for line in lines:
        stripped = line.strip()
        if stripped and CHAPTER_RE.match(stripped):
            flush()
            current_title = stripped
            continue
        current_lines.append(line)
    flush()

    if not chapters:
        chapters.append(Chapter(chapter_id="001", title="正文", text=text))
    return chapters


def run(project_dir: Path, config_path: Path | None = None, chapter: str | None = None) -> Path:
    config = load_config(project_dir, config_path)
    input_path = project_path(project_dir, config["ingest"]["input_file"])
    output_path = project_path(project_dir, config["ingest"]["chapters_file"])

    logger.info("Reading novel: {}", input_path)
    text = normalize_text(read_text(input_path))
    chapters = split_chapters(text)
    write_jsonl(output_path, chapters)
    logger.info("Wrote chapters: {} ({} chapters, {} chars)", output_path, len(chapters), len(text))
    return output_path

