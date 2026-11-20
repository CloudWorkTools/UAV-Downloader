"""Validate word-timed subtitles from the managed Speaches service."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata
import wave

from uav_downloader.subtitles import asr, engine

_RATE = 16000
_VAD_WINDOW_SECONDS = 120
_CONTEXT_SECONDS = 2
_CUE_CYCLE_LIMIT = 8
_TEXT_CYCLE = re.compile(r'(.)\1{11,}|(.{2,64}?)\2{5,}')


def _request_window(source, start, end, settings, folder, log_path, cancel_check):
    engine._check_cancel(cancel_check)
    engine._ensure_asr_temp_space(folder, (end - start) * 2)
    wav_path = os.path.join(folder, 'chunk.wav')
    request_path = Path(folder, 'request.json')
    response_path = Path(folder, 'response.json')
    source.setpos(start)
    samples = source.readframes(end - start)
    if len(samples) != (end - start) * 2:
        raise engine.SubtitleError('音訊讀取不完整')
    with wave.open(wav_path, 'wb') as chunk:
        chunk.setparams(source.getparams())
        chunk.writeframes(samples)
    descriptor = os.open(request_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as request:
        json.dump(settings, request)
    response_path.unlink(missing_ok=True)
    engine._run_process(
        [sys.executable, '-m', asr.__name__, str(request_path), wav_path, str(response_path)],
        log_path, cancel_check, timeout_seconds=1850, disk_guard_path=folder)
    with response_path.open('rb') as response:
        encoded = response.read(asr.MAX_RESPONSE_BYTES + 1)
    if len(encoded) > asr.MAX_RESPONSE_BYTES:
        raise engine.SubtitleError('辨識回應過大')
    try:
        envelope = json.loads(encoded)
    except ValueError:
        raise engine.SubtitleError('辨識回應無法讀取') from None
    if not isinstance(envelope, dict) or envelope.get('ok') is not True:
        raise engine.SubtitleError('辨識服務未傳回結果')
    return envelope.get('payload')


def _time_pair(item, limit, *, allow_equal=False):
    if not isinstance(item, dict):
        raise ValueError('時間戳格式無效')
    start, end = item.get('start'), item.get('end')
    if (type(start) not in (int, float) or type(end) not in (int, float)
            or not 0 <= start <= end <= limit or (start == end and not allow_equal)):
        raise ValueError('時間戳範圍無效')
    return start, end


def _speech_regions(source, request):
    intervals = []
    frames = source.getnframes()
    step = _VAD_WINDOW_SECONDS * _RATE
    context = _CONTEXT_SECONDS * _RATE
    for position in range(0, frames, step):
        start, end = max(0, position - context), min(frames, position + step + context)
        try:
            payload = request(start, end, _speech_timestamps=True)
            if not isinstance(payload, list) or len(payload) > 10000:
                raise ValueError('語音區間格式無效')
            for item in payload:
                begin, finish = _time_pair(item, (end - start) * 1000 / _RATE)
                first = start + round(begin * _RATE / 1000)
                last = start + round(finish * _RATE / 1000)
                if first >= last:
                    raise ValueError('語音區間小於一個音訊取樣')
                intervals.append((first, last))
        except (engine.SubtitleCancelled, engine.SubtitleStorageError):
            raise
        except (ValueError, TypeError):
            raise asr.RecognitionQualityError(start / _RATE, end / _RATE) from None
        except (engine.SubtitleError, OSError):
            raise engine.SubtitleError(
                f'原片第 {start / _RATE:.3f} 至 {end / _RATE:.3f} 秒無法確認語音區間') from None
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _normalized(text):
    return ''.join(char for char in unicodedata.normalize('NFKC', text).casefold()
                   if not char.isspace() and not unicodedata.category(char).startswith('P'))


def _word_timed_cues(payload, start, end):
    segments = asr._stage_segments(payload, (end - start) / _RATE)
    words = payload.get('words')
    if not isinstance(words, list) or len(words) > 10000:
        raise ValueError('辨識結果缺少逐詞時間')
    timed_words = []
    previous_start = previous_end = 0
    for item in words:
        begin, finish = _time_pair(item, (end - start) / _RATE, allow_equal=True)
        text = item.get('word')
        if (not isinstance(text, str) or not text.strip() or '\x00' in text
                or begin < previous_start or finish < previous_end):
            raise ValueError('逐詞文字或順序無效')
        previous_start, previous_end = begin, finish
        normalized = _normalized(text)
        if normalized:
            timed_words.append((begin, finish, normalized))

    cues = []
    word_index = 0
    for _, _, text in segments:
        normalized = _normalized(text)
        if not normalized:
            continue
        if _TEXT_CYCLE.search(normalized):
            raise ValueError('同一組文字在單則字幕中重複過多')
        matched = 0
        first_word = word_index
        while matched < len(normalized) and word_index < len(timed_words):
            begin, _, word = timed_words[word_index]
            if not normalized.startswith(word, matched):
                raise ValueError('逐詞文字與字幕不符')
            if word_index > first_word and begin - timed_words[word_index - 1][1] > _CONTEXT_SECONDS:
                raise ValueError('單則字幕的詞跨越長時間空檔')
            matched += len(word)
            word_index += 1
        if matched != len(normalized):
            raise ValueError('逐詞時間未涵蓋字幕全文')
        begin = start / _RATE + timed_words[first_word][0]
        finish = start / _RATE + timed_words[word_index - 1][1]
        if round(begin * 1000) >= round(finish * 1000):
            raise ValueError('字幕時間無效')
        if cues and (begin, finish, normalized) == (cues[-1][0], cues[-1][1], cues[-1][3]):
            raise ValueError('同一時間出現重複字幕')
        cues.append((begin, finish, text, normalized))
    if not cues or word_index != len(timed_words):
        raise ValueError('語音區間缺少可確認的字幕')
    return cues


def _join_windows(previous, incoming, seam):
    # Compare both observations before assigning unmatched cues to the core they touch.
    left = right = 0
    merged = []
    while left < len(previous) and right < len(incoming):
        old = previous[left]
        new = incoming[right]
        if old[1] <= new[0]:
            if (old[1] <= seam <= new[0] and old[3] == new[3] and new[0] - old[1] <= 0.25
                    and left + 1 == len(previous) and right == 0):
                raise ValueError('分塊交界無法確認是否是同一句字幕')
            if old[0] < seam:
                merged.append(old)
            left += 1
        elif new[1] <= old[0]:
            if new[1] > seam:
                merged.append(new)
            right += 1
        else:
            if old[3] != new[3]:
                raise ValueError('分塊交界的文字無法確認')
            merged.append(old if old[0] < seam else new)
            left += 1
            right += 1
    merged.extend(cue for cue in previous[left:] if cue[0] < seam)
    merged.extend(cue for cue in incoming[right:] if cue[1] > seam)
    if any(round(old[1] * 1000) > round(new[0] * 1000) and old[3] != new[3]
           for old, new in zip(merged, merged[1:])):
        raise ValueError('分塊交界的文字無法確認')
    return merged


def _check_cue_cycles(cues):
    labels = []
    previous_end = 0
    for begin, end, _, label in cues:
        if begin - previous_end > 2 * _CONTEXT_SECONDS:
            labels.clear()
        previous_end = end
        labels.append(label)
        for period in range(1, min(len(labels) // 3, _CUE_CYCLE_LIMIT) + 1):
            count = 6 if period == 1 else 3
            if len(labels) >= period * count and labels[-period * count:] == labels[-period:] * count:
                raise ValueError('連續字幕形成重複週期')


def transcribe_managed(settings, wav_path, output_base, log_path, cancel_check, progress_callback):
    cues = []
    with wave.open(wav_path, 'rb') as source, tempfile.TemporaryDirectory(
            prefix='asr-library-', dir=os.path.dirname(wav_path)) as folder:
        if source.getnchannels() != 1 or source.getsampwidth() != 2 or source.getframerate() != _RATE:
            raise engine.SubtitleError('辨識音訊必須為 16 kHz 單聲道 PCM')

        def request(start, end, **options):
            return _request_window(source, start, end, dict(settings, **options),
                                   folder, log_path, cancel_check)

        regions = _speech_regions(source, request)
        frames = source.getnframes()
        previous_end = 0
        install_model = True
        for index, (first, last) in enumerate(regions):
            next_start = regions[index + 1][0] if index + 1 < len(regions) else frames
            left_bound = (previous_end + first) // 2 if index else 0
            right_bound = (last + next_start) // 2 if index + 1 < len(regions) else frames
            region_start = len(cues)
            context = _CONTEXT_SECONDS * _RATE
            step = asr.CHUNK_SECONDS * _RATE
            for position in range(first, last, step):
                core_end = min(last, position + step)
                start = max(left_bound, position - context)
                end = min(right_bound, core_end + context)
                keep = len(cues)
                if position > first:
                    while keep > region_start and cues[keep - 1][1] > start / _RATE:
                        keep -= 1
                for attempt in range(2):
                    try:
                        parameters = {'timestamp_granularities': ['word'], 'vad_filter': 'true'}
                        if attempt:
                            parameters['temperature'] = '0.2'
                        payload = request(start, end, _parameters=parameters, _install_model=install_model)
                        install_model = False
                        accepted = _word_timed_cues(payload, start, end)
                        if not any(begin < core_end / _RATE and end > position / _RATE
                                   for begin, end, _, _ in accepted):
                            raise ValueError('語音區間沒有對應的逐詞時間')
                        if position > first:
                            accepted = _join_windows(cues[keep:], accepted, position / _RATE)
                        if not accepted:
                            raise ValueError('分塊交界缺少可確認的字幕')
                        _check_cue_cycles(cues[max(0, keep - _CUE_CYCLE_LIMIT * 3):keep] + accepted)
                    except (engine.SubtitleCancelled, engine.SubtitleStorageError):
                        raise
                    except (engine.SubtitleError, OSError, ValueError) as exc:
                        if attempt == 0:
                            engine._notify(progress_callback, 'asr_tail_retry', None)
                            continue
                        if isinstance(exc, ValueError):
                            raise asr.RecognitionQualityError(position / _RATE, core_end / _RATE) from None
                        raise engine.SubtitleError(
                            f'原片第 {position / _RATE:.3f} 至 {core_end / _RATE:.3f} 秒辨識服務失敗') from None
                    break
                cues[keep:] = accepted
                if len(cues) > engine.MAX_WHISPER_CUES:
                    raise engine.SubtitleError('辨識 API 字幕片段過多')
                engine._notify(progress_callback, 'transcribe_ja', int(core_end * 100 / frames))
            previous_end = last
        engine._check_cancel(cancel_check)
    return asr._write_transcription(output_base, [(begin, end, text) for begin, end, text, _ in cues])
