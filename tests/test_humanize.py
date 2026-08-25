"""Pure unit tests for the humanizer splitter + pacing (ticket 14).

No DB/redis fixtures needed — `split_reply`/`typing_delay_seconds` are
stdlib-only, so these run without the async app fixtures used elsewhere.
"""

from app.config import Settings
from app.conversations.humanize import split_reply, typing_delay_seconds


def test_splits_on_blank_lines():
    text = "First bubble.\n\nSecond bubble.\n\nThird bubble."
    assert split_reply(text, max_parts=4) == [
        "First bubble.",
        "Second bubble.",
        "Third bubble.",
    ]


def test_wall_of_text_falls_back_to_sentence_split():
    sentence = "This is a reasonably long sentence about widgets and orders. "
    text = (sentence * 6).strip()  # one paragraph, well over 280 chars
    parts = split_reply(text, max_parts=4)

    assert len(parts) > 1
    for part in parts:
        assert len(part) <= 280
        assert not part.startswith(" ")
        assert not part.endswith(" ")
    # Reassembling (with single spaces) recovers the original text exactly.
    assert " ".join(parts) == text


def test_code_fence_is_never_split():
    text = (
        "Here is the fix:\n\n"
        "```python\n"
        "def add(a, b):\n\n"
        "    return a + b\n"
        "```\n\n"
        "Let me know if that works."
    )
    parts = split_reply(text, max_parts=4)

    fence_parts = [p for p in parts if "```" in p]
    assert len(fence_parts) == 1
    fence = fence_parts[0]
    assert fence.count("```") == 2
    assert "def add(a, b):" in fence
    assert "return a + b" in fence


def test_cap_merges_tail_into_last_part():
    text = "\n\n".join(f"Bubble {i}." for i in range(1, 7))  # 6 paragraphs
    parts = split_reply(text, max_parts=4)

    assert len(parts) == 4
    assert parts[:3] == ["Bubble 1.", "Bubble 2.", "Bubble 3."]
    assert "Bubble 4." in parts[3]
    assert "Bubble 5." in parts[3]
    assert "Bubble 6." in parts[3]


def test_empty_and_whitespace_input_yields_no_parts():
    assert split_reply("", max_parts=4) == []
    assert split_reply("   \n\n  \t ", max_parts=4) == []


def test_delay_clamps_to_minimum_for_short_parts():
    settings = Settings(
        humanize_chars_per_sec=35.0,
        humanize_min_delay_seconds=0.8,
        humanize_max_delay_seconds=4.0,
    )
    assert typing_delay_seconds("Hi", settings) == 0.8


def test_delay_clamps_to_maximum_for_long_parts():
    settings = Settings(
        humanize_chars_per_sec=35.0,
        humanize_min_delay_seconds=0.8,
        humanize_max_delay_seconds=4.0,
    )
    long_part = "x" * 1000
    assert typing_delay_seconds(long_part, settings) == 4.0


def test_delay_scales_linearly_between_bounds():
    settings = Settings(
        humanize_chars_per_sec=35.0,
        humanize_min_delay_seconds=0.8,
        humanize_max_delay_seconds=4.0,
    )
    part = "x" * 70  # 70 / 35 = 2.0s, within [0.8, 4.0]
    assert typing_delay_seconds(part, settings) == 2.0
