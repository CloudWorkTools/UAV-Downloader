"""Shared value types for subtitle stage inputs and outputs."""
from dataclasses import dataclass


@dataclass(frozen=True)
class SpeechInterval:
    start: float
    end: float


@dataclass(frozen=True)
class SubtitleSegment:
    segment_id: str
    start: float
    end: float
    text: str
