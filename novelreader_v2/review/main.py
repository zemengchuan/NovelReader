from __future__ import annotations

from pathlib import Path

from loguru import logger

from novelreader_v2.common.config import load_config, project_path
from novelreader_v2.common.jsonio import read_json, read_jsonl, write_text
from novelreader_v2.common.schema import CharacterBible, Chapter, PlanItem


TEMPLATE_STYLE_MARKERS = ("自然中文对白", "自然中文旁白", "按上下文自然朗读")


def _selected_chapters(chapters: list[Chapter], chapter: str | None) -> list[Chapter]:
    if not chapter:
        return chapters
    rows = [row for row in chapters if row.chapter_id == chapter]
    if not rows:
        raise ValueError(f"chapter {chapter!r} not found")
    return rows


def _speaker_candidates(bible: CharacterBible) -> set[str]:
    rows = {"旁白", "未具名男声", "未具名女声", "未知角色"}
    rows.update(bible.characters.keys())
    return rows


def _issue(line_id: int, level: str, message: str, item: PlanItem) -> str:
    text = item.text.replace("\n", " ")
    if len(text) > 120:
        text = text[:117] + "..."
    return (
        f"### [{level}] id={line_id} speaker={item.speaker} kind={item.kind}\n\n"
        f"- 问题：{message}\n"
        f"- 置信度：{item.confidence}\n"
        f"- reason：{item.reason or '空'}\n"
        f"- text：{text}\n"
    )


def _review_items(chapter: Chapter, items: list[PlanItem], bible: CharacterBible, config: dict) -> list[str]:
    issues: list[str] = []
    min_confidence = float(config["review"].get("min_confidence", 0.7))
    min_style_chars = int(config["review"].get("min_style_chars", 8))
    candidates = _speaker_candidates(bible)

    for item in items:
        if item.text not in chapter.text:
            issues.append(_issue(item.id, "HIGH", "text 未在章节原文中找到", item))
        if item.speaker not in candidates:
            issues.append(_issue(item.id, "HIGH", "speaker 不在 Character Bible 或允许未知角色中", item))
        if item.confidence < min_confidence:
            issues.append(_issue(item.id, "MEDIUM", f"confidence 低于 {min_confidence}", item))
        if item.needs_review:
            issues.append(_issue(item.id, "MEDIUM", "needs_review=true", item))
        if not item.reason.strip():
            issues.append(_issue(item.id, "LOW", "reason 为空", item))
        if len(item.style_prompt.strip()) < min_style_chars:
            issues.append(_issue(item.id, "LOW", "style_prompt 过短", item))
        if any(marker in item.style_prompt for marker in TEMPLATE_STYLE_MARKERS):
            issues.append(_issue(item.id, "LOW", "style_prompt 疑似模板化", item))
    return issues


def run(project_dir: Path, config_path: Path | None = None, chapter: str | None = None) -> Path:
    config = load_config(project_dir, config_path)
    chapters = [Chapter.model_validate(row) for row in read_jsonl(project_path(project_dir, config["ingest"]["chapters_file"]))]
    selected = _selected_chapters(chapters, chapter)
    bible = CharacterBible.model_validate(read_json(project_path(project_dir, config["bible"]["output_file"])))
    reports_dir = project_path(project_dir, config["review"]["output_dir"])
    plans_dir = project_path(project_dir, config["planner"]["output_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)

    last_output = reports_dir
    for chapter_obj in selected:
        plan_path = plans_dir / f"{chapter_obj.chapter_id}.plan.jsonl"
        items = [PlanItem.model_validate(row) for row in read_jsonl(plan_path)]
        issues = _review_items(chapter_obj, items, bible, config)
        body = [
            f"# Review {chapter_obj.chapter_id} {chapter_obj.title}",
            "",
            "## Summary",
            "",
            f"- plan items: {len(items)}",
            f"- issues: {len(issues)}",
            f"- characters: {', '.join(bible.characters.keys()) or '无'}",
            "",
            "## Issues",
            "",
        ]
        body.extend(issues or ["未发现工程校验问题。"])
        output_path = reports_dir / f"{chapter_obj.chapter_id}.review.md"
        write_text(output_path, "\n".join(body) + "\n")
        logger.info("Wrote review: {} ({} issues)", output_path, len(issues))
        last_output = output_path
    return last_output

