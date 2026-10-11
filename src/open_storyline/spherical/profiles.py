"""Reusable directing profiles for common spherical-editing scenarios."""

from __future__ import annotations

from open_storyline.spherical.direction_schemas import (
    EditingProfile,
    VerticalOutputBehavior,
)
from open_storyline.spherical.errors import SphericalValidationError


def _profile(
    profile_id: str,
    display_name: str,
    *,
    shot_min: float,
    shot_max: float,
    close: tuple[float, float],
    medium: tuple[float, float],
    wide: tuple[float, float],
    max_pan: float,
    speaker: float,
    reactions: float,
    establishing: float,
    vertical: VerticalOutputBehavior,
    speaker_threshold: float,
    track_threshold: float,
    nadir: float = -55.0,
    seam_width: float = 12.0,
    music: str = "preserve_existing_timeline_music",
    subtitles: str = "preserve_existing_timeline_subtitles",
) -> EditingProfile:
    return EditingProfile(
        profile_id=profile_id,
        display_name=display_name,
        preferred_shot_duration_min_seconds=shot_min,
        preferred_shot_duration_max_seconds=shot_max,
        close_fov_range=close,
        medium_fov_range=medium,
        wide_fov_range=wide,
        maximum_pan_speed_degrees_per_second=max_pan,
        speaker_following_strength=speaker,
        reaction_shot_frequency=reactions,
        establishing_shot_frequency=establishing,
        vertical_output_behavior=vertical,
        probable_speaker_threshold=speaker_threshold,
        minimum_track_confidence=track_threshold,
        avoid_nadir_below_pitch=nadir,
        stitch_seam_yaws=[180.0],
        stitch_seam_zone_width=seam_width,
        music_handoff_behavior=music,
        subtitle_handoff_behavior=subtitles,
    )


EDITING_PROFILES: dict[str, EditingProfile] = {
    profile.profile_id: profile
    for profile in (
        _profile(
            "round-table-podcast",
            "Round-table podcast",
            shot_min=3.0,
            shot_max=12.0,
            close=(48.0, 65.0),
            medium=(68.0, 88.0),
            wide=(100.0, 125.0),
            max_pan=32.0,
            speaker=0.95,
            reactions=0.22,
            establishing=0.12,
            vertical=VerticalOutputBehavior.ACTIVE_SUBJECT,
            speaker_threshold=0.68,
            track_threshold=0.45,
        ),
        _profile(
            "family-travel-vlog",
            "Family travel vlog",
            shot_min=2.0,
            shot_max=8.0,
            close=(52.0, 72.0),
            medium=(72.0, 96.0),
            wide=(102.0, 135.0),
            max_pan=45.0,
            speaker=0.65,
            reactions=0.35,
            establishing=0.35,
            vertical=VerticalOutputBehavior.GROUP_WHEN_UNCERTAIN,
            speaker_threshold=0.66,
            track_threshold=0.40,
        ),
        _profile(
            "theme-park-vlog",
            "Theme-park vlog",
            shot_min=1.4,
            shot_max=6.0,
            close=(50.0, 70.0),
            medium=(70.0, 98.0),
            wide=(105.0, 140.0),
            max_pan=58.0,
            speaker=0.55,
            reactions=0.45,
            establishing=0.48,
            vertical=VerticalOutputBehavior.ACTIVE_SUBJECT,
            speaker_threshold=0.65,
            track_threshold=0.38,
        ),
        _profile(
            "walking-tour",
            "Walking tour",
            shot_min=4.0,
            shot_max=16.0,
            close=(58.0, 76.0),
            medium=(78.0, 104.0),
            wide=(108.0, 145.0),
            max_pan=28.0,
            speaker=0.35,
            reactions=0.08,
            establishing=0.55,
            vertical=VerticalOutputBehavior.CENTER_CROP,
            speaker_threshold=0.70,
            track_threshold=0.42,
        ),
        _profile(
            "restaurant-review",
            "Restaurant review",
            shot_min=2.5,
            shot_max=9.0,
            close=(48.0, 68.0),
            medium=(70.0, 92.0),
            wide=(98.0, 125.0),
            max_pan=35.0,
            speaker=0.82,
            reactions=0.38,
            establishing=0.28,
            vertical=VerticalOutputBehavior.ACTIVE_SUBJECT,
            speaker_threshold=0.67,
            track_threshold=0.43,
        ),
        _profile(
            "vehicle-conversation",
            "Vehicle conversation",
            shot_min=3.5,
            shot_max=12.0,
            close=(52.0, 70.0),
            medium=(72.0, 94.0),
            wide=(100.0, 126.0),
            max_pan=24.0,
            speaker=0.90,
            reactions=0.28,
            establishing=0.10,
            vertical=VerticalOutputBehavior.ACTIVE_SUBJECT,
            speaker_threshold=0.70,
            track_threshold=0.48,
            nadir=-45.0,
            seam_width=15.0,
        ),
    )
}


def list_editing_profiles() -> list[EditingProfile]:
    return list(EDITING_PROFILES.values())


def get_editing_profile(profile_id: str) -> EditingProfile:
    try:
        return EDITING_PROFILES[profile_id]
    except KeyError as exc:
        raise SphericalValidationError(
            "Unknown spherical editing profile.",
            details={
                "profile_id": profile_id,
                "available_profiles": sorted(EDITING_PROFILES),
            },
        ) from exc
