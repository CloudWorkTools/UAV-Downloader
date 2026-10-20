"""LocalAI VAD adapter for 16 kHz mono PCM audio."""
from __future__ import annotations

from array import array
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import wave

import requests

from uav_downloader.subtitles.models import SpeechInterval
from uav_downloader.subtitles.profiles import ProfileSnapshot, capability_report, resolve_credential

SAMPLE_RATE = 16000
CHUNK_SECONDS = 30
CHUNK_OVERLAP_SECONDS = 0.25
MAX_RESPONSE_BYTES = 1024 * 1024


def _profile(snapshot: ProfileSnapshot) -> dict:
    try:
        profile = json.loads(snapshot.profile_json)
    except (TypeError, json.JSONDecodeError):
        raise ValueError("VAD 設定快照格式錯誤") from None
    if not isinstance(profile, dict) or "vad" not in capability_report(snapshot)["stages"]:
        raise ValueError("所選設定不支援語音活動偵測")
    if profile.get("provider") != "localai":
        raise ValueError("所選模型服務不支援獨立語音活動偵測")
    parameters = profile.get("parameters")
    if not isinstance(parameters, dict) or set(parameters) - {
            "threshold", "min_silence_duration_ms", "speech_pad_ms"}:
        raise ValueError("VAD 模型參數不受支援")
    if parameters:
        raise ValueError("LocalAI VAD 參數只能在服務端設定；請設定模型別名後再執行")
    if not profile.get("base_url") or not profile.get("model"):
        raise ValueError("請設定 VAD 模型服務與模型別名")
    return profile


def _validate_intervals(payload: object, duration: float) -> list[SpeechInterval]:
    if not isinstance(payload, dict) or not isinstance(payload.get("segments"), list):
        raise ValueError("VAD API 必須回傳 segments")
    segments = payload["segments"]
    if len(segments) > 10000:
        raise ValueError("VAD API 回應片段過多")
    intervals = []
    previous_start = -1.0
    for item in segments:
        if not isinstance(item, dict):
            raise ValueError("VAD API 片段格式錯誤")
        start, end = item.get("start"), item.get("end")
        try:
            finite = math.isfinite(start) and math.isfinite(end)
        except (OverflowError, TypeError):
            finite = False
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or not finite
                or not 0 <= start < end <= duration or start < previous_start):
            raise ValueError("VAD API 必須回傳合法的秒單位時間區間")
        intervals.append(SpeechInterval(float(start), float(end)))
        previous_start = start
    return intervals


def detect_speech(audio_path: str, profile_snapshot: ProfileSnapshot,
                  cancel_check=None) -> list[SpeechInterval]:
    profile = _profile(profile_snapshot)
    url = profile["base_url"].rstrip("/")
    if not url.endswith("/v1"):
        url += "/v1"
    url += "/vad"
    key = resolve_credential(profile_snapshot.credential_ref, profile["base_url"])
    headers = {"Authorization": "Bearer " + key} if key else {}
    output = []
    from uav_downloader.subtitles import engine
    with wave.open(audio_path, "rb") as audio, tempfile.TemporaryDirectory(
            prefix="subtitle-vad-", dir=os.path.dirname(audio_path)) as folder:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getframerate() != SAMPLE_RATE:
            raise ValueError("VAD 音訊必須為 16 kHz 單聲道 PCM")
        total_frames = audio.getnframes()
        frames_per_chunk = SAMPLE_RATE * CHUNK_SECONDS
        overlap_frames = int(CHUNK_OVERLAP_SECONDS * SAMPLE_RATE)
        for index, core_start in enumerate(range(0, total_frames, frames_per_chunk)):
            engine._check_cancel(cancel_check)
            offset = max(0, core_start - overlap_frames)
            stop = min(total_frames, core_start + frames_per_chunk + overlap_frames)
            frames = stop - offset
            audio.setpos(offset)
            samples = array("h", audio.readframes(frames))
            if samples.itemsize != 2:
                raise ValueError("不支援的 PCM 樣本格式")
            chunk_duration = frames / SAMPLE_RATE
            request_path, response_path = (os.path.join(folder, f"{index}.{suffix}")
                                           for suffix in ("request.json", "response.json"))
            descriptor = os.open(request_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as request_file:
                json.dump({"url": url, "headers": headers, "model": profile["model"],
                           "audio": [sample / 32768.0 for sample in samples]}, request_file)
            try:
                engine._run_process(
                    [sys.executable, "-m", __name__, request_path, response_path],
                    os.path.join(folder, "worker.log"), cancel_check,
                    timeout_seconds=900, disk_guard_path=folder)
                envelope = json.loads(Path(response_path).read_bytes())
                if not isinstance(envelope, dict) or envelope.get("ok") is not True:
                    raise ValueError(envelope.get("error", "VAD API 回應無效"))
                intervals = _validate_intervals(envelope.get("payload"), chunk_duration)
            except (OSError, ValueError, TypeError) as exc:
                raise engine.SubtitleError(str(exc)) from None
            output.extend(SpeechInterval(item.start + offset / SAMPLE_RATE,
                                         item.end + offset / SAMPLE_RATE)
                          for item in intervals)
    merged = []
    for interval in sorted(output, key=lambda item: (item.start, item.end)):
        if merged and interval.start <= merged[-1].end + CHUNK_OVERLAP_SECONDS:
            merged[-1] = SpeechInterval(merged[-1].start, max(merged[-1].end, interval.end))
        else:
            merged.append(interval)
    return merged


def _request(settings_path: str):
    request = json.loads(Path(settings_path).read_bytes())
    with requests.Session() as session:
        session.trust_env = False
        with session.post(request["url"], headers=request["headers"],
                          json={"model": request["model"], "audio": request["audio"]},
                          timeout=(10, 900), allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise ValueError(f"VAD API 回應 HTTP {response.status_code}")
            content = bytearray()
            for block in response.iter_content(65536):
                content.extend(block)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise ValueError("VAD API 回應過大")
    return json.loads(content)


def _main():
    try:
        result = {"ok": True, "payload": _request(sys.argv[1])}
    except Exception as exc:
        message = str(exc) if isinstance(exc, ValueError) and str(exc).startswith("VAD API") else (
            "VAD API 連線或回應失敗，請確認服務、模型與金鑰")
        result = {"ok": False, "error": message}
    Path(sys.argv[2]).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    _main()
