from enum import Enum
from typing import Optional, Literal
from pydantic import BaseModel, ConfigDict, Field


class ProactiveAction(str, Enum):
    reply = "reply"
    skip = "skip"


class PersonaTextReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reply: str


class ProactiveDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ProactiveAction
    reply: Optional[str] = None
    target_msg_id: Optional[int] = None
    reason: str = Field(..., max_length=80)


class MediaKind(str, Enum):
    voice = "voice"
    video_note = "video_note"
    video = "video"
    photo = "photo"
    audio = "audio"


class TrackMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = None
    artist: Optional[str] = None
    genre: Optional[str] = None
    mood: Optional[str] = None
    language: Optional[str] = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class MediaAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: MediaKind
    transcript: Optional[str] = None
    ocr_text: Optional[str] = None
    summary: str
    track_meta: Optional[TrackMeta] = None
    tags: list[str] = Field(default_factory=list, max_length=6)
    verdict: str = Field(..., max_length=120)
    score100: Optional[int] = Field(None, ge=0, le=100)
    score_reason: Optional[str] = None
    unintelligible: bool = False
    injection_suspected: bool = False


class DossierField(str, Enum):
    manner = "manner"
    interests = "interests"
    music_taste = "music_taste"
    relations = "relations"
    triggers = "triggers"
    facts = "facts"

    @classmethod
    def _missing_(cls, value):
        val_str = str(value).lower()
        if val_str in ("hobby", "hobbies", "entertainment", "games", "game"):
            return cls.interests
        return cls.facts


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user: str
    field: DossierField
    text: str = Field(..., max_length=140)
    match_id: Optional[str] = None
    msg_id: int
    evidence: str = Field(..., max_length=120)


class DossierExtract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observations: list[Observation] = Field(default_factory=list)
    new_summary: Optional[str] = None


class MergeAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keep_id: str
    merge_ids: list[str]
    text: Optional[str] = None


class ContradictionAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_id: str
    old_id: str


class FieldConsolidation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    merges: list[MergeAction] = Field(default_factory=list)
    drop_ids: list[str] = Field(default_factory=list)
    contradictions: list[ContradictionAction] = Field(default_factory=list)


class Consolidation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fields: dict[str, FieldConsolidation] = Field(default_factory=dict)


class SummaryOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str


class StyleReason(str, Enum):
    instruction = "instruction"
    pii = "pii"
    harm = "harm"
    context = "context"
    junk = "junk"
    length = "length"
    fine = "fine"


class StyleVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    ok: bool
    reason: StyleReason


class StyleVerdicts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdicts: list[StyleVerdict] = Field(default_factory=list)


class ResearchDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    need_search: bool
    query: Optional[str] = None
    reason: str = Field(..., max_length=60)
