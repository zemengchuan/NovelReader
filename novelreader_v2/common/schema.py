from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Chapter(BaseModel):
    chapter_id: str
    title: str
    text: str


class CharacterProfile(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    gender: str = "未知"
    identity: str = ""
    personality: list[str] = Field(default_factory=list)
    speech_style: str = ""
    voice_style: str = ""
    relations: dict[str, str] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)
    confidence: float = 0.0


class CharacterBible(BaseModel):
    characters: dict[str, CharacterProfile] = Field(default_factory=dict)
    aliases: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class ChapterState(BaseModel):
    chapter_id: str
    title: str = ""
    present_characters: list[str] = Field(default_factory=list)
    temporary_characters: dict[str, str] = Field(default_factory=dict)
    relationship_updates: dict[str, str] = Field(default_factory=dict)
    summary: str = ""
    unresolved: list[str] = Field(default_factory=list)


class PlanItem(BaseModel):
    id: int
    chapter_id: str
    chunk_id: int
    source_start: int = -1
    source_end: int = -1
    speaker: str
    text: str
    kind: Literal["narration", "dialogue"] = "narration"
    emotion: str = ""
    style_prompt: str
    delivery: dict[str, str | int | float | bool | list[str]] = Field(default_factory=dict)
    pause_after_ms: int = 500
    confidence: float = 0.5
    reason: str = ""
    needs_review: bool = False

