from __future__ import annotations

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    id: str
    chapter_id: str
    text: str
    start_char: int = -1
    end_char: int = -1
    source: str = "llm"


class CharacterNode(BaseModel):
    id: str
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    gender: str = "未知"
    role: str = ""
    personality: list[str] = Field(default_factory=list)
    speech_style: str = ""
    voice_style: str = ""
    confidence: float = 0.0
    evidence_ids: list[str] = Field(default_factory=list)


class RelationEdge(BaseModel):
    id: str
    source: str
    target: str
    type: str = ""
    attitude: str = ""
    confidence: float = 0.0
    evidence_ids: list[str] = Field(default_factory=list)


class StoryEvent(BaseModel):
    id: str
    chapter_id: str
    summary: str
    participants: list[str] = Field(default_factory=list)
    impact: str = ""
    confidence: float = 0.0
    evidence_ids: list[str] = Field(default_factory=list)


class QuoteEvidence(BaseModel):
    id: str
    chapter_id: str
    text: str
    candidate_speakers: list[str] = Field(default_factory=list)
    speaker: str = ""
    confidence: float = 0.0
    reason: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class ChapterGraph(BaseModel):
    chapter_id: str
    title: str = ""
    summary: str = ""
    characters: dict[str, CharacterNode] = Field(default_factory=dict)
    relations: list[RelationEdge] = Field(default_factory=list)
    events: list[StoryEvent] = Field(default_factory=list)
    quotes: list[QuoteEvidence] = Field(default_factory=list)
    evidence: dict[str, Evidence] = Field(default_factory=dict)
    unresolved: list[str] = Field(default_factory=list)


class BookGraph(BaseModel):
    characters: dict[str, CharacterNode] = Field(default_factory=dict)
    aliases: dict[str, str] = Field(default_factory=dict)
    relations: list[RelationEdge] = Field(default_factory=list)
    events: list[StoryEvent] = Field(default_factory=list)
    quotes: list[QuoteEvidence] = Field(default_factory=list)
    evidence: dict[str, Evidence] = Field(default_factory=dict)
    chapter_summaries: dict[str, str] = Field(default_factory=dict)
    unresolved: list[str] = Field(default_factory=list)


class RetrievedContext(BaseModel):
    chapter_id: str
    chunk_id: int
    source_start: int
    source_end: int
    context_before: str
    target_text: str
    context_after: str
    candidate_speakers: list[str] = Field(default_factory=list)
    characters: list[CharacterNode] = Field(default_factory=list)
    relations: list[RelationEdge] = Field(default_factory=list)
    recent_events: list[StoryEvent] = Field(default_factory=list)
    quote_evidence: list[QuoteEvidence] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    previous_summary: str = ""

