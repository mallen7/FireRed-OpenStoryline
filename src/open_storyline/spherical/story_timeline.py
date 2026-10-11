"""Normalize existing OpenStoryline outputs onto a spherical source timeline."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from open_storyline.spherical.analysis_schemas import (
    SphericalAnalysis,
    SubjectSample,
    VisibilityState,
)
from open_storyline.spherical.direction_schemas import (
    NormalizedStoryTimeline,
    TimelineSignal,
    TimelineSignalKind,
    TranscriptTurn,
    TranscriptWord,
)
from open_storyline.spherical.manifests import sha256_json


def _milliseconds(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return default


def _clamp_interval(
    start: float,
    end: float,
    duration: float,
    *,
    minimum_duration: float = 0.001,
) -> tuple[float, float] | None:
    bounded_start = max(0.0, min(duration, start))
    bounded_end = max(0.0, min(duration, end))
    if bounded_end <= bounded_start:
        bounded_end = min(duration, bounded_start + minimum_duration)
    if bounded_end <= bounded_start:
        return None
    return round(bounded_start, 6), round(bounded_end, 6)


def _tokens(text: str) -> list[str]:
    words = re.findall(r"\S+", text)
    if len(words) > 1:
        return words
    return [character for character in text if not character.isspace()]


def _timestamp_pairs(raw: Any) -> list[tuple[float, float]]:
    pairs: list[tuple[float, float]] = []
    if not isinstance(raw, list):
        return pairs
    for item in raw:
        if isinstance(item, dict):
            start, end = item.get("start"), item.get("end")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            start, end = item[0], item[1]
        else:
            continue
        try:
            start_seconds = float(start) / 1000.0
            end_seconds = float(end) / 1000.0
        except (TypeError, ValueError):
            continue
        if end_seconds > start_seconds:
            pairs.append((start_seconds, end_seconds))
    return pairs


def _clip_intervals(
    clips: list[dict[str, Any]],
    duration: float,
) -> dict[str, tuple[float, float]]:
    result: dict[str, tuple[float, float]] = {}
    for clip in clips:
        clip_id = str(clip.get("clip_id", "") or "").strip()
        if not clip_id:
            continue
        source = clip.get("source_ref") or {}
        start = _milliseconds(source.get("start"), 0.0)
        end = _milliseconds(source.get("end"), start)
        if end <= start:
            end = start + _milliseconds(source.get("duration"), 0.0)
        interval = _clamp_interval(start, end, duration)
        if interval is not None:
            result[clip_id] = interval
    return result


def _transcript_items(
    asr_infos: list[dict[str, Any]],
    duration: float,
) -> tuple[list[TranscriptTurn], list[TranscriptWord], list[str]]:
    turns: list[TranscriptTurn] = []
    words: list[TranscriptWord] = []
    warnings: list[str] = []
    for clip_index, info in enumerate(asr_infos):
        clip_id = str(info.get("clip_id", "") or f"clip-{clip_index}")
        source = info.get("source_ref") or {}
        source_start = _milliseconds(source.get("start"), 0.0)
        source_end = _milliseconds(source.get("end"), duration)
        if source_end <= source_start:
            source_end = source_start + _milliseconds(source.get("duration"), duration)
        text = str(info.get("asr_text", "") or "").strip()
        word_pairs = _timestamp_pairs(info.get("asr_timestamps"))
        tokens = _tokens(text)
        clip_word_ids: list[str] = []
        for word_index, (relative_start, relative_end) in enumerate(word_pairs):
            interval = _clamp_interval(
                source_start + relative_start,
                source_start + relative_end,
                duration,
            )
            if interval is None:
                warnings.append(
                    f"Dropped out-of-range ASR word timestamp for {clip_id}."
                )
                continue
            token = tokens[word_index] if word_index < len(tokens) else f"token-{word_index + 1}"
            word_id = f"word-{sha256_json([clip_id, word_index, interval, token])[:14]}"
            words.append(
                TranscriptWord(
                    word_id=word_id,
                    start_seconds=interval[0],
                    end_seconds=interval[1],
                    text=token,
                    clip_id=clip_id,
                )
            )
            clip_word_ids.append(word_id)

        sentence_info = info.get("asr_sentence_info")
        if not isinstance(sentence_info, list):
            sentence_info = []
        for sentence_index, sentence in enumerate(sentence_info):
            if not isinstance(sentence, dict):
                continue
            sentence_text = str(
                sentence.get("text")
                or sentence.get("sentence")
                or sentence.get("text_seg")
                or ""
            ).strip()
            if not sentence_text:
                continue
            relative_start = _milliseconds(sentence.get("start"), 0.0)
            relative_end = _milliseconds(sentence.get("end"), 0.0)
            if relative_end <= relative_start:
                embedded = _timestamp_pairs(sentence.get("timestamp"))
                if embedded:
                    relative_start, relative_end = embedded[0][0], embedded[-1][1]
            if relative_end <= relative_start:
                relative_end = min(
                    max(0.001, source_end - source_start),
                    relative_start + max(0.25, len(sentence_text) / 12.0),
                )
            interval = _clamp_interval(
                source_start + relative_start,
                source_start + relative_end,
                duration,
            )
            if interval is None:
                warnings.append(
                    f"Dropped out-of-range ASR sentence timestamp for {clip_id}."
                )
                continue
            sentence_word_ids = [
                word.word_id
                for word in words
                if word.clip_id == clip_id
                and word.end_seconds > interval[0]
                and word.start_seconds < interval[1]
            ]
            turn_id = f"turn-{sha256_json([clip_id, sentence_index, interval, sentence_text])[:14]}"
            confidence = sentence.get("confidence")
            turns.append(
                TranscriptTurn(
                    turn_id=turn_id,
                    start_seconds=interval[0],
                    end_seconds=interval[1],
                    text=sentence_text,
                    word_ids=sentence_word_ids,
                    clip_id=clip_id,
                    confidence=(
                        max(0.0, min(1.0, float(confidence)))
                        if confidence is not None
                        else None
                    ),
                )
            )
        if text and not any(turn.clip_id == clip_id for turn in turns):
            interval = _clamp_interval(source_start, source_end, duration)
            if interval is None:
                interval = _clamp_interval(0.0, duration, duration)
            if interval is not None:
                turns.append(
                    TranscriptTurn(
                        turn_id=f"turn-{sha256_json([clip_id, interval, text])[:14]}",
                        start_seconds=interval[0],
                        end_seconds=interval[1],
                        text=text,
                        word_ids=clip_word_ids,
                        clip_id=clip_id,
                    )
                )
    turns.sort(key=lambda turn: (turn.start_seconds, turn.end_seconds, turn.turn_id))
    words.sort(key=lambda word: (word.start_seconds, word.end_seconds, word.word_id))
    if asr_infos and not turns:
        warnings.append("Existing ASR output contained no usable transcript timing.")
    if turns and not words:
        warnings.append("Transcript turns are aligned, but word timestamps were unavailable.")
    return turns, words, warnings


def _sample_end(sample: SubjectSample, following: SubjectSample | None, duration: float) -> float:
    if following is not None:
        return min(duration, max(sample.timestamp_seconds, following.timestamp_seconds))
    return min(duration, sample.timestamp_seconds + 0.25)


def _activity_signals(
    raw: Iterable[dict[str, Any]],
    *,
    kind: TimelineSignalKind,
    duration: float,
) -> list[TimelineSignal]:
    signals: list[TimelineSignal] = []
    for index, item in enumerate(raw):
        try:
            start = float(item.get("start_seconds", 0.0))
            end = float(item.get("end_seconds", start))
        except (TypeError, ValueError):
            continue
        interval = _clamp_interval(start, end, duration)
        if interval is None:
            continue
        confidence = item.get("confidence")
        signals.append(
            TimelineSignal(
                signal_id=f"{kind.value}-{sha256_json([index, item])[:14]}",
                kind=kind,
                start_seconds=interval[0],
                end_seconds=interval[1],
                confidence=(
                    max(0.0, min(1.0, float(confidence)))
                    if confidence is not None
                    else None
                ),
                subject_id=item.get("subject_id"),
                metadata={
                    key: value
                    for key, value in item.items()
                    if key not in {"start_seconds", "end_seconds", "confidence", "subject_id"}
                },
            )
        )
    return signals


def normalize_story_timeline(
    analysis: SphericalAnalysis,
    *,
    duration_seconds: float,
    asr_infos: list[dict[str, Any]] | None = None,
    split_shots: list[dict[str, Any]] | None = None,
    clip_captions: list[dict[str, Any]] | None = None,
    group_scripts: list[dict[str, Any]] | None = None,
    timeline_tracks: dict[str, Any] | None = None,
    mouth_activity: list[dict[str, Any]] | None = None,
    audio_activity: list[dict[str, Any]] | None = None,
) -> NormalizedStoryTimeline:
    """Connect existing OpenStoryline results to one absolute spherical timeline."""

    asr = list(asr_infos or [])
    clips = list(split_shots or [])
    captions = list(clip_captions or [])
    scripts = list(group_scripts or [])
    duration = float(duration_seconds)
    turns, words, warnings = _transcript_items(asr, duration)
    clip_intervals = _clip_intervals(clips, duration)
    signals: list[TimelineSignal] = []

    for turn in turns:
        signals.append(
            TimelineSignal(
                signal_id=f"transcript-{turn.turn_id}",
                kind=TimelineSignalKind.TRANSCRIPT,
                start_seconds=turn.start_seconds,
                end_seconds=turn.end_seconds,
                confidence=turn.confidence,
                clip_id=turn.clip_id,
                text=turn.text,
            )
        )
    for word in words:
        signals.append(
            TimelineSignal(
                signal_id=f"signal-{word.word_id}",
                kind=TimelineSignalKind.WORD,
                start_seconds=word.start_seconds,
                end_seconds=word.end_seconds,
                confidence=word.confidence,
                clip_id=word.clip_id,
                text=word.text,
            )
        )

    boundaries = {0.0, duration}
    boundaries.update(
        boundary
        for boundary in analysis.shot_boundaries_seconds
        if 0.0 <= boundary <= duration
    )
    for start, end in clip_intervals.values():
        boundaries.update((start, end))
    ordered_boundaries = sorted(boundaries)
    for index, (start, end) in enumerate(zip(ordered_boundaries, ordered_boundaries[1:])):
        if end <= start:
            continue
        matching_clips = [
            clip_id
            for clip_id, interval in clip_intervals.items()
            if interval[1] > start and interval[0] < end
        ]
        signals.append(
            TimelineSignal(
                signal_id=f"scene-{index:04d}",
                kind=TimelineSignalKind.SCENE,
                start_seconds=start,
                end_seconds=end,
                metadata={"clip_ids": matching_clips},
            )
        )

    for track in analysis.subject_tracks:
        face_candidate = "face-candidate" in track.identity_evidence.get("labels", [])
        for sample_index, sample in enumerate(track.samples):
            if sample.visibility != VisibilityState.VISIBLE:
                continue
            following = (
                track.samples[sample_index + 1]
                if sample_index + 1 < len(track.samples)
                else None
            )
            end = _sample_end(sample, following, duration)
            if end <= sample.timestamp_seconds:
                continue
            metadata = {
                "yaw": sample.yaw,
                "pitch": sample.pitch,
                "angular_width": sample.angular_width,
                "angular_height": sample.angular_height,
                "visibility": sample.visibility.value,
            }
            signals.append(
                TimelineSignal(
                    signal_id=f"subject-{track.subject_id}-{sample_index:04d}",
                    kind=TimelineSignalKind.SUBJECT,
                    start_seconds=sample.timestamp_seconds,
                    end_seconds=end,
                    confidence=sample.confidence,
                    subject_id=track.subject_id,
                    metadata=metadata,
                )
            )
            if face_candidate:
                signals.append(
                    TimelineSignal(
                        signal_id=f"face-{track.subject_id}-{sample_index:04d}",
                        kind=TimelineSignalKind.FACE_ACTIVITY,
                        start_seconds=sample.timestamp_seconds,
                        end_seconds=end,
                        confidence=sample.confidence,
                        subject_id=track.subject_id,
                        metadata={"source": "face-candidate track label"},
                    )
                )

    supplied_audio = _activity_signals(
        audio_activity or [],
        kind=TimelineSignalKind.AUDIO_ACTIVITY,
        duration=duration,
    )
    if supplied_audio:
        signals.extend(supplied_audio)
    else:
        signals.extend(
            TimelineSignal(
                signal_id=f"audio-{turn.turn_id}",
                kind=TimelineSignalKind.AUDIO_ACTIVITY,
                start_seconds=turn.start_seconds,
                end_seconds=turn.end_seconds,
                confidence=turn.confidence or 0.65,
                text=turn.text,
                metadata={"source": "existing ASR turn"},
            )
            for turn in turns
        )
    signals.extend(
        _activity_signals(
            mouth_activity or [],
            kind=TimelineSignalKind.MOUTH_ACTIVITY,
            duration=duration,
        )
    )

    for index, caption in enumerate(captions):
        clip_id = str(caption.get("clip_id", "") or "") or None
        interval = clip_intervals.get(clip_id or "", (0.0, duration))
        text = str(caption.get("caption", "") or "").strip() or None
        signals.append(
            TimelineSignal(
                signal_id=f"understanding-{index:04d}",
                kind=TimelineSignalKind.CLIP_UNDERSTANDING,
                start_seconds=interval[0],
                end_seconds=interval[1],
                clip_id=clip_id,
                text=text,
                metadata={
                    key: value
                    for key, value in caption.items()
                    if key not in {"clip_id", "caption"}
                },
            )
        )
    for index, script in enumerate(scripts):
        group_id = str(script.get("group_id", "") or "")
        raw_text = str(script.get("raw_text", "") or "").strip()
        signals.append(
            TimelineSignal(
                signal_id=f"script-{index:04d}",
                kind=TimelineSignalKind.SCRIPT,
                start_seconds=0.0,
                end_seconds=duration,
                text=raw_text or None,
                metadata={"group_id": group_id, "subtitle_units": script.get("subtitle_units", [])},
            )
        )
    if timeline_tracks:
        signals.append(
            TimelineSignal(
                signal_id="existing-timeline-plan",
                kind=TimelineSignalKind.TIMELINE_PLAN,
                start_seconds=0.0,
                end_seconds=duration,
                metadata={"tracks": timeline_tracks},
            )
        )

    signals.sort(
        key=lambda signal: (
            signal.start_seconds,
            signal.end_seconds,
            signal.kind.value,
            signal.signal_id,
        )
    )
    if not turns:
        warnings.append(
            "No transcript turns were supplied; direction can use visual and scene signals only."
        )
    if not mouth_activity:
        warnings.append(
            "Mouth-movement evidence was unavailable; speaker scores disclose a zero mouth signal."
        )
    source_summary = {
        "asr_clips": len(asr),
        "transcript_turns": len(turns),
        "words": len(words),
        "scene_intervals": sum(signal.kind == TimelineSignalKind.SCENE for signal in signals),
        "subject_tracks": len(analysis.subject_tracks),
        "face_activity_signals": sum(
            signal.kind == TimelineSignalKind.FACE_ACTIVITY for signal in signals
        ),
        "mouth_activity_signals": sum(
            signal.kind == TimelineSignalKind.MOUTH_ACTIVITY for signal in signals
        ),
        "audio_activity_signals": sum(
            signal.kind == TimelineSignalKind.AUDIO_ACTIVITY for signal in signals
        ),
        "clip_understanding_items": len(captions),
        "script_items": len(scripts),
        "timeline_plan_items": 1 if timeline_tracks else 0,
    }
    identity_payload = {
        "analysis_id": analysis.analysis_id,
        "asset_id": analysis.asset_id,
        "duration_seconds": duration,
        "turns": [turn.model_dump(mode="json") for turn in turns],
        "words": [word.model_dump(mode="json") for word in words],
        "signals": [signal.model_dump(mode="json") for signal in signals],
    }
    return NormalizedStoryTimeline(
        timeline_id=f"story-timeline-{sha256_json(identity_payload)[:16]}",
        analysis_id=analysis.analysis_id,
        asset_id=analysis.asset_id,
        duration_seconds=duration,
        transcript_turns=turns,
        words=words,
        scene_boundaries_seconds=ordered_boundaries,
        signals=signals,
        source_summary=source_summary,
        warnings=list(dict.fromkeys(warnings)),
    )
