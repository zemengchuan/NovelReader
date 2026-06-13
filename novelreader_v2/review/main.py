from __future__ import annotations

import re
from pathlib import Path

from loguru import logger

from novelreader_v2.common.config import load_config, project_path
from novelreader_v2.common.graph_schema import BookGraph
from novelreader_v2.common.jsonio import read_json, read_jsonl, write_text
from novelreader_v2.common.schema import Chapter, PlanItem


TEMPLATE_STYLE_MARKERS = (
    "自然中文对白",
    "自然中文旁白",
    "自然朗读",
    "平稳朗读",
    "贴合上下文",
)

PUNCT_ONLY_RE = re.compile(r"^[\s「」『』“”\"'‘’…\.。！？!?，,、；;：:（）()【】\[\]《》<>-]+$")


def _selected_chapters(chapters: list[Chapter], chapter: str | None) -> list[Chapter]:
    if not chapter:
        return chapters
    rows = [row for row in chapters if row.chapter_id == chapter]
    if not rows:
        raise ValueError(f"chapter {chapter!r} not found")
    return rows


def _load_book_graph(project_dir: Path, config: dict) -> BookGraph:
    path = project_path(project_dir, config["graph_index"]["book_graph_file"])
    if path.exists():
        return BookGraph.model_validate(read_json(path))
    return BookGraph()


def _speaker_candidates(graph: BookGraph) -> set[str]:
    rows = {"旁白", "未具名男声", "未具名女声", "未知角色"}
    rows.update(character.canonical_name for character in graph.characters.values())
    return rows


def _short_text(text: str, limit: int = 120) -> str:
    text = text.replace("\n", " ")
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def _issue(line_id: int, level: str, message: str, item: PlanItem) -> str:
    return (
        f"### [{level}] id={line_id} speaker={item.speaker} kind={item.kind}\n\n"
        f"- 问题：{message}\n"
        f"- 置信度：{item.confidence}\n"
        f"- 情绪：{item.emotion or '空'}\n"
        f"- 强度：{item.intensity}\n"
        f"- reason：{item.reason or '空'}\n"
        f"- evidence_ids：{', '.join(item.evidence_ids) or '空'}\n"
        f"- text：{_short_text(item.text)}\n"
    )


def _gap_issue(chapter: Chapter, previous: PlanItem, current: PlanItem, gap_text: str) -> str:
    return (
        f"### [HIGH] source gap after id={previous.id} before id={current.id}\n\n"
        f"- 问题：plan 存在未覆盖原文，可能导致合成音频跳内容。\n"
        f"- 坐标：{previous.source_end}..{current.source_start}\n"
        f"- previous：id={previous.id} speaker={previous.speaker} text={_short_text(previous.text, 80)}\n"
        f"- current：id={current.id} speaker={current.speaker} text={_short_text(current.text, 80)}\n"
        f"- gap：{_short_text(gap_text, 160)}\n"
    )


def _is_punctuation_only(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped) and bool(PUNCT_ONLY_RE.fullmatch(stripped))


def _looks_like_quote_fragment(text: str) -> bool:
    stripped = text.strip()
    if _is_punctuation_only(stripped):
        return True
    if stripped in {"」", "」", "…」", "……」", "「", "『", "』"}:
        return True
    if len(stripped) <= 3 and any(mark in stripped for mark in "「」『』…"):
        return True
    return False


def _style_is_generic(style: str) -> bool:
    style = style.strip()
    if not style:
        return True
    if any(marker in style for marker in TEMPLATE_STYLE_MARKERS) and len(style) < 24:
        return True
    return False


def _review_items(chapter: Chapter, items: list[PlanItem], graph: BookGraph, config: dict) -> list[str]:
    issues: list[str] = []
    min_confidence = float(config["review"].get("min_confidence", 0.7))
    min_style_chars = int(config["review"].get("min_style_chars", 12))
    candidates = _speaker_candidates(graph)
    narrator = str(config["planner"].get("narrator_name", "旁白"))

    for item in items:
        if item.text not in chapter.text:
            issues.append(_issue(item.id, "HIGH", "text 未在章节原文中找到", item))
        if _looks_like_quote_fragment(item.text):
            issues.append(_issue(item.id, "HIGH", "疑似孤立引号、省略号或纯标点碎片，应该并入相邻朗读单元", item))
        if item.speaker not in candidates:
            issues.append(_issue(item.id, "HIGH", "speaker 不在图谱人物或允许的未知角色中", item))
        if item.kind == "dialogue" and item.speaker == narrator:
            issues.append(_issue(item.id, "MEDIUM", "dialogue 被标成旁白，可能是 speaker 归因不足", item))
        if item.confidence < min_confidence:
            issues.append(_issue(item.id, "MEDIUM", f"confidence 低于 {min_confidence}", item))
        if item.needs_review:
            issues.append(_issue(item.id, "MEDIUM", "needs_review=true", item))
        if not item.reason.strip():
            issues.append(_issue(item.id, "LOW", "reason 为空", item))
        if len(item.style_prompt.strip()) < min_style_chars:
            issues.append(_issue(item.id, "LOW", "style_prompt 过短", item))
        if _style_is_generic(item.style_prompt):
            issues.append(_issue(item.id, "LOW", "style_prompt 疑似模板化", item))
        if not item.emotion.strip():
            issues.append(_issue(item.id, "LOW", "emotion 为空", item))
        if item.kind == "dialogue" and not item.delivery:
            issues.append(_issue(item.id, "LOW", "dialogue 缺少 delivery 细节", item))
        if item.kind == "dialogue" and item.speaker not in {narrator, "未知角色"} and not item.evidence_ids:
            issues.append(_issue(item.id, "MEDIUM", "具名对白缺少 evidence_ids", item))
        if not item.span_ids:
            issues.append(_issue(item.id, "LOW", "span_ids 为空，无法追溯 source span", item))

    valid_items = sorted(
        [item for item in items if item.source_start >= 0 and item.source_end >= item.source_start],
        key=lambda item: (item.source_start, item.source_end),
    )
    for previous, current in zip(valid_items, valid_items[1:]):
        if current.source_start <= previous.source_end:
            continue
        gap_text = chapter.text[previous.source_end : current.source_start]
        if gap_text.strip():
            issues.append(_gap_issue(chapter, previous, current, gap_text))
    return issues


def run(project_dir: Path, config_path: Path | None = None, chapter: str | None = None) -> Path:
    config = load_config(project_dir, config_path)
    chapters = [Chapter.model_validate(row) for row in read_jsonl(project_path(project_dir, config["ingest"]["chapters_file"]))]
    selected = _selected_chapters(chapters, chapter)
    graph = _load_book_graph(project_dir, config)
    reports_dir = project_path(project_dir, config["review"]["output_dir"])
    plans_dir = project_path(project_dir, config["planner"]["output_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)

    last_output = reports_dir
    for chapter_obj in selected:
        plan_path = plans_dir / f"{chapter_obj.chapter_id}.plan.jsonl"
        items = [PlanItem.model_validate(row) for row in read_jsonl(plan_path)]
        issues = _review_items(chapter_obj, items, graph, config)
        body = [
            f"# Review {chapter_obj.chapter_id} {chapter_obj.title}",
            "",
            "## Summary",
            "",
            f"- plan items: {len(items)}",
            f"- issues: {len(issues)}",
            f"- graph characters: {', '.join(character.canonical_name for character in graph.characters.values()) or '无'}",
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
