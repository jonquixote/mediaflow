"""Explicit even output dimensions and SAR handling.

Requirement markers: SIZE-4 (never upscale, preserve frame rate/display
aspect/SAR) and SIZE-5 (fit within the cap box, floor to even, hard caps).
"""

from __future__ import annotations

import pytest

from mediaflow.plan import PlanError, compute_dimensions


@pytest.mark.parametrize(
    "width,height,sar,cap_w,cap_h,expected",
    [
        # Already inside the cap: dimensions are preserved.
        (1920, 800, "1:1", 1920, 1080, (1920, 800, False)),
        # A 2.39:1 feature stored as 1920x804 (letterbox removed) fits.
        (1920, 804, "1:1", 1920, 1080, (1920, 804, False)),
        (1440, 1080, "1:1", 1920, 1080, (1440, 1080, False)),
        # Never upscale a small source.
        (640, 480, "1:1", 1920, 1080, (640, 480, False)),
        # Standard downscales.
        (3840, 2160, "1:1", 1920, 1080, (1920, 1080, True)),
        (1920, 1080, "1:1", 1280, 720, (1280, 720, True)),
        (1920, 800, "1:1", 1280, 720, (1280, 532, True)),
        # Width is the binding cap.
        (3840, 1600, "1:1", 1920, 1080, (1920, 800, True)),
    ],
)
def test_compute_dimensions_cases(width, height, sar, cap_w, cap_h, expected):
    """SIZE-5: fit the cap box without upscaling."""
    assert compute_dimensions(width, height, sar, cap_w, cap_h) == expected


def test_compute_dimensions_uses_sar_display_width():
    """SIZE-4/SIZE-5: anamorphic 1440x1080 4:3 displays as 1920x1080."""
    # Display width is 1440 * 4/3 = 1920, so it fits a 1920x1080 cap unchanged.
    assert compute_dimensions(1440, 1080, "4:3", 1920, 1080) == (1440, 1080, False)
    # A tighter display cap scales the storage frame while preserving SAR.
    width, height, scaled = compute_dimensions(1440, 1080, "4:3", 1280, 720)
    assert (width, height, scaled) == (960, 720, True)
    assert width * 4 / 3 <= 1280
    assert height <= 720


def test_compute_dimensions_handles_odd_sar_and_odd_dimensions():
    """SIZE-5: odd SAR and odd source sizes still produce even, capped output."""
    width, height, scaled = compute_dimensions(1919, 1079, "32:27", 1920, 1080)
    assert width % 2 == 0 and height % 2 == 0
    assert width <= 1920 and height <= 1080
    assert scaled is True

    width, height, _ = compute_dimensions(1001, 777, "1:1", 1920, 1080)
    assert (width, height) == (1000, 776)
    assert width % 2 == 0 and height % 2 == 0


def test_compute_dimensions_never_exceeds_either_cap():
    """SIZE-5: both caps are hard limits for many shapes."""
    for source in ((4096, 2160), (3840, 1600), (1919, 1081), (10000, 100)):
        for cap in ((1920, 1080), (1280, 720), (1440, 1080), (720, 576)):
            width, height, _ = compute_dimensions(*source, "1:1", *cap)
            assert width <= cap[0], (source, cap, width, height)
            assert height <= cap[1], (source, cap, width, height)
            assert width % 2 == 0 and height % 2 == 0


def test_compute_dimensions_invalid_sar_defaults_and_invalid_inputs():
    """SIZE-5: malformed SAR falls back safely; impossible caps are an error."""
    assert compute_dimensions(1920, 1080, "N/A", 1920, 1080) == (1920, 1080, False)
    assert compute_dimensions(1920, 1080, "0:1", 1920, 1080) == (1920, 1080, False)
    with pytest.raises(PlanError):
        compute_dimensions(0, 1080, "1:1", 1920, 1080)
    with pytest.raises(PlanError):
        compute_dimensions(1920, 1080, "1:1", 1, 1080)
