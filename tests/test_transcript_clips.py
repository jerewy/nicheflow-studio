from __future__ import annotations

import textwrap

from nicheflow_studio.processing import transcript_clips as tc


SAMPLE_SRT = textwrap.dedent(
    """\
    1
    00:00:00,000 --> 00:00:02,000
    >> This is the first sentence.

    2
    00:00:02,000 --> 00:00:04,000
    Here is the second one [music] with noise.

    3
    00:00:04,000 --> 00:00:06,000
    Here is the second one [music] with noise.

    4
    00:00:06,000 --> 00:00:09,000
    >> A new speaker starts talking now

    5
    00:00:09,000 --> 00:00:11,500
    and keeps going until it ends.

    6
    00:00:20,000 --> 00:00:23,000
    After a long gap a fresh sentence appears.
    """
)


def _sentences() -> list[tc.TranscriptSentence]:
    return tc.sentences_from_srt(SAMPLE_SRT)


def test_parse_srt_reads_spans_and_text() -> None:
    cues = tc.parse_srt(SAMPLE_SRT)
    assert len(cues) == 6
    assert cues[0].start == 0.0 and cues[0].end == 2.0
    assert cues[5].start == 20.0 and cues[5].end == 23.0


def test_dedup_consecutive_cues_collapses_and_extends_end() -> None:
    cues = tc.parse_srt(SAMPLE_SRT)
    deduped = tc.dedup_consecutive_cues(cues)
    # Cues 2 and 3 are identical text -> one cue spanning 2.0..6.0.
    matches = [c for c in deduped if "second one" in c.text]
    assert len(matches) == 1
    assert matches[0].start == 2.0 and matches[0].end == 6.0


def test_build_sentences_splits_and_cleans() -> None:
    sentences = _sentences()
    assert len(sentences) == 4
    # Speaker markers and [music] annotations are stripped.
    assert sentences[0].text == "This is the first sentence."
    assert ">>" not in sentences[0].text
    assert "[music]" not in sentences[1].text
    assert sentences[1].text == "Here is the second one with noise."
    # Speaker change + no end punctuation until cue 5 -> one merged sentence.
    assert sentences[2].start == 6.0 and sentences[2].end == 11.5
    assert sentences[2].text.startswith("A new speaker")
    # The long silent gap begins a fresh sentence at 20s.
    assert sentences[3].start == 20.0


def test_snap_to_sentences_expands_to_whole_sentences() -> None:
    sentences = _sentences()
    # A rough mid-sentence selection 3.0..7.0 should snap out to 2.0..11.5.
    window = tc.snap_to_sentences(sentences, 3.0, 7.0)
    assert window.start == 2.0
    assert window.end == 11.5
    assert "second one" in window.text and "new speaker" in window.text.lower()


def test_snap_to_sentences_single_sentence() -> None:
    sentences = _sentences()
    window = tc.snap_to_sentences(sentences, 1.0, 1.5)
    assert window.start == 0.0 and window.end == 2.0


def test_clip_window_around_grows_forward_to_target() -> None:
    sentences = _sentences()
    window = tc.clip_window_around(sentences, 3.0, target_seconds=8.0)
    # Anchor sentence (2..6) is only 4s; grows forward into 6..11.5 first.
    assert window.start == 2.0
    assert window.end == 11.5
    assert window.duration >= 8.0


def test_clip_window_around_does_not_cross_a_long_gap() -> None:
    sentences = _sentences()
    # Anchoring on the last speaker line, a big target must NOT pull in the
    # sentence across the 8.5s silent gap (that would bury dead air in the clip).
    window = tc.clip_window_around(sentences, 8.0, target_seconds=40.0)
    assert window.end == 11.5  # never reaches the 20s sentence across the gap
    assert "fresh sentence" not in window.text  # the post-gap sentence is excluded


def test_find_moment_and_window_for_text() -> None:
    sentences = _sentences()
    assert tc.find_moment(sentences, "second one") is sentences[1]
    assert tc.find_moment(sentences, "nothing here") is None

    window = tc.clip_window_for_text(sentences, "new speaker", target_seconds=8.0)
    assert window is not None
    assert window.start <= 6.0 and window.end == 11.5
    assert "new speaker" in window.text.lower()


def test_clip_window_for_text_missing_returns_none() -> None:
    assert tc.clip_window_for_text(_sentences(), "unobtainium") is None


def test_min_seconds_default_clears_campaign_floor() -> None:
    # The campaign requires "over 7 seconds"; our default floor keeps a margin.
    assert tc.DEFAULT_MIN_SECONDS >= 8.0


def test_caption_cues_for_window_filters_cleans_and_clamps() -> None:
    cues = tc.parse_srt(SAMPLE_SRT)
    window = tc.ClipWindow(start=2.0, end=11.5, text="")
    caption_cues = tc.caption_cues_for_window(cues, window)

    # The pre-window (0..2) and post-gap (20..23) cues are excluded.
    joined = " ".join(cue.text for cue in caption_cues)
    assert "first sentence" not in joined
    assert "fresh sentence" not in joined
    # Annotations and speaker markers are stripped from caption text.
    assert "[music]" not in joined and ">>" not in joined
    # Cues stay in order and never overlap (each end <= next start).
    for earlier, later in zip(caption_cues, caption_cues[1:]):
        assert earlier.end <= later.start


def test_caption_srt_for_window_is_zero_based(tmp_path) -> None:
    cues = tc.parse_srt(SAMPLE_SRT)
    window = tc.ClipWindow(start=2.0, end=11.5, text="")
    out = tc.caption_srt_for_window(cues, window, tmp_path / "clip.srt")

    content = out.read_text(encoding="utf-8")
    # First cue in the window starts at 2.0s absolute -> 0.0s clip-relative.
    assert content.startswith("1\n00:00:00,000 -->")
    # Nothing should exceed the 9.5s window length.
    assert "00:00:10," not in content and "00:00:11," not in content
