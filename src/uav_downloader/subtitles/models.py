"""Shared value types for subtitle stage inputs and outputs."""
from dataclasses import dataclass


class StageRequestError(ValueError):
    """Safe external-stage failure with an explicit retry classification."""

    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        if not isinstance(retryable, bool):
            raise TypeError('retryable must be bool')
        self.retryable = retryable


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


@dataclass(frozen=True)
class SpeechChunk:
    index: int
    core_start_frame: int
    core_end_frame: int
    start_frame: int
    end_frame: int


@dataclass(frozen=True)
class TranscriptionChunk:
    index: int
    interval_index: int
    core_start_frame: int
    core_end_frame: int
    start_frame: int
    end_frame: int
