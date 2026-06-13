from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from loguru import logger

from novelreader_v2.common.config import load_config, project_path
from novelreader_v2.common.jsonio import read_json, read_jsonl, write_json
from novelreader_v2.common.ollama import ollama_generate_json
from novelreader_v2.common.schema import CharacterBible, CharacterProfile, Chapter, ChapterState


BIBLE_SCHEMA = {
    "type": "object",
    "properties": {
        "characters": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "gender": {"type": "string"},
                    "identity": {"type": "string"},
                    "personality": {"type": "array", "items": {"type": "string"}},
                    "speech_style": {"type": "string"},
                    "voice_style": {"type": "string"},
                    "relations": {"type": "object", "additionalProperties": {"type": "string"}},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number"},
                },
                "required": ["name"],
            },
        },
        "aliases": {"type": "object", "additionalProperties": {"type": "string"}},
        "chapter_state": {
            "type": "object",
            "properties": {
                "present_characters": {"type": "array", "items": {"type": "string"}},
                "temporary_characters": {"type": "object", "additionalProperties": {"type": "string"}},
                "relationship_updates": {"type": "object", "additionalProperties": {"type": "string"}},
                "summary": {"type": "string"},
                "unresolved": {"type": "array", "items": {"type": "string"}},
            },
        },
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["characters", "chapter_state"],
}


SYSTEM_PROMPT = """你是中文小说 Character Bible 编辑器，只输出 JSON。
任务：根据当前章节更新人物卡和章节状态。

原则：
- 只记录真实人物或明确的临时未具名角色，不要把旁白、声音、房间、时候当人物。
- 人名必须保持中文原文，不要拼音化。
- aliases 只写确实指向同一人物的称号、别名、简称。
- personality、speech_style、voice_style 必须有小说依据，别写空泛套话。
- relations 写人物之间的长期关系或本章变化。
- chapter_state 写本章出现人物、临时角色、关系变化和摘要。
- 不要输出 Markdown，不要解释。"""


STOP_NAMES = {"旁白", "一个", "这个", "那个", "声音", "房间", "时候", "自己", "少年", "少女", "男子", "女子"}


def _load_bible(path: Path) -> CharacterBible:
    if path.exists():
        return CharacterBible.model_validate(read_json(path))
    return CharacterBible()


def _chapter_by_id(chapters: list[Chapter], chapter: str | None) -> list[Chapter]:
    if not chapter:
        return chapters
    selected = [row for row in chapters if row.chapter_id == chapter]
    if not selected:
        raise ValueError(f"chapter {chapter!r} not found")
    return selected


def _brief_bible(bible: CharacterBible) -> str:
    rows: list[dict[str, Any]] = []
    for name, profile in bible.characters.items():
        rows.append(
            {
                "name": name,
                "aliases": profile.aliases[:5],
                "gender": profile.gender,
                "identity": profile.identity,
                "personality": profile.personality[:5],
                "speech_style": profile.speech_style,
                "relations": dict(list(profile.relations.items())[:5]),
            }
        )
    return json.dumps({"characters": rows, "aliases": bible.aliases}, ensure_ascii=False)


def _merge_list(old: list[str], new: list[str], limit: int = 20) -> list[str]:
    rows: list[str] = []
    for item in old + new:
        item = str(item).strip()
        if item and item not in rows:
            rows.append(item)
    return rows[:limit]


def _valid_name(name: str) -> bool:
    name = name.strip()
    if not (2 <= len(name) <= 12):
        return False
    if name in STOP_NAMES:
        return False
    return bool(re.search(r"[\u4e00-\u9fff]", name))


def _merge_profile(old: CharacterProfile | None, raw: dict[str, Any]) -> CharacterProfile | None:
    name = str(raw.get("name", "")).strip()
    if not _valid_name(name):
        return None
    base = old or CharacterProfile(name=name)
    return CharacterProfile(
        name=name,
        aliases=_merge_list(base.aliases, [str(x).strip() for x in raw.get("aliases", []) if str(x).strip()], 20),
        gender=str(raw.get("gender") or base.gender or "未知"),
        identity=str(raw.get("identity") or base.identity),
        personality=_merge_list(base.personality, [str(x).strip() for x in raw.get("personality", [])], 20),
        speech_style=str(raw.get("speech_style") or base.speech_style),
        voice_style=str(raw.get("voice_style") or base.voice_style),
        relations={**base.relations, **{str(k): str(v) for k, v in dict(raw.get("relations", {})).items()}},
        evidence=_merge_list(base.evidence, [str(x).strip() for x in raw.get("evidence", [])], 30),
        confidence=float(raw.get("confidence") or base.confidence or 0.0),
    )


def _merge_bible(bible: CharacterBible, data: dict[str, Any]) -> CharacterBible:
    for _, raw in dict(data.get("characters", {})).items():
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", "")).strip()
        existing_name = bible.aliases.get(name, name)
        old = bible.characters.get(existing_name)
        profile = _merge_profile(old, raw)
        if not profile:
            continue
        bible.characters[profile.name] = profile
        for alias in profile.aliases:
            if alias and alias not in STOP_NAMES:
                bible.aliases[alias] = profile.name

    for alias, name in dict(data.get("aliases", {})).items():
        alias = str(alias).strip()
        name = str(name).strip()
        if alias and name in bible.characters:
            bible.aliases[alias] = name
    bible.notes = _merge_list(bible.notes, [str(x).strip() for x in data.get("notes", [])], 50)
    return bible


def _fallback_state(chapter: Chapter, bible: CharacterBible, reason: str) -> ChapterState:
    return ChapterState(
        chapter_id=chapter.chapter_id,
        title=chapter.title,
        present_characters=list(bible.characters.keys()),
        summary=f"Character Bible 更新失败，需复核：{reason}",
        unresolved=[reason],
    )


def _make_prompt(chapter: Chapter, bible: CharacterBible, window_chars: int) -> str:
    sample = chapter.text[:window_chars]
    return (
        f"已有 Character Bible 摘要：\n{_brief_bible(bible)}\n\n"
        f"当前章节：{chapter.chapter_id} {chapter.title}\n\n"
        f"当前章节文本窗口：\n{sample}\n"
    )


def run(project_dir: Path, config_path: Path | None = None, chapter: str | None = None) -> Path:
    config = load_config(project_dir, config_path)
    chapters_path = project_path(project_dir, config["ingest"]["chapters_file"])
    bible_path = project_path(project_dir, config["bible"]["output_file"])
    state_dir = project_path(project_dir, config["bible"]["state_dir"])
    state_dir.mkdir(parents=True, exist_ok=True)

    chapters = [Chapter.model_validate(row) for row in read_jsonl(chapters_path)]
    selected = _chapter_by_id(chapters, chapter)
    bible = _load_bible(bible_path)
    ollama = config["ollama"]
    window_chars = int(config["bible"].get("window_chars", 6000))

    for chapter_obj in selected:
        logger.info("Updating Character Bible from chapter {} {}", chapter_obj.chapter_id, chapter_obj.title)
        try:
            data = ollama_generate_json(
                url=ollama["url"],
                model=ollama["model"],
                system=SYSTEM_PROMPT,
                prompt=_make_prompt(chapter_obj, bible, window_chars),
                schema=BIBLE_SCHEMA,
                temperature=float(ollama["temperature"]),
                num_predict=int(ollama["num_predict"]),
                keep_alive=str(ollama["keep_alive"]),
                timeout_seconds=int(ollama["timeout_seconds"]),
                num_ctx=int(ollama.get("num_ctx", 0)) or None,
                task_name=f"bible.{chapter_obj.chapter_id}",
                max_retries=1,
            )
            bible = _merge_bible(bible, data)
            raw_state = data.get("chapter_state", {}) if isinstance(data, dict) else {}
            state = ChapterState(
                chapter_id=chapter_obj.chapter_id,
                title=chapter_obj.title,
                present_characters=[str(x) for x in raw_state.get("present_characters", [])],
                temporary_characters={str(k): str(v) for k, v in dict(raw_state.get("temporary_characters", {})).items()},
                relationship_updates={str(k): str(v) for k, v in dict(raw_state.get("relationship_updates", {})).items()},
                summary=str(raw_state.get("summary", "")),
                unresolved=[str(x) for x in raw_state.get("unresolved", [])],
            )
        except Exception as exc:
            logger.warning("Bible update failed for chapter {}: {}", chapter_obj.chapter_id, exc)
            state = _fallback_state(chapter_obj, bible, str(exc))

        write_json(state_dir / f"{chapter_obj.chapter_id}.chapter_state.json", state)

    write_json(bible_path, bible)
    logger.info("Wrote Character Bible: {} ({} characters)", bible_path, len(bible.characters))
    return bible_path

