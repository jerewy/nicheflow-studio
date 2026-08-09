from __future__ import annotations

from nicheflow_studio.processing import transcript_clips as tc
from nicheflow_studio.processing import virality


def test_score_text_rewards_money_superlative_and_celebrity() -> None:
    hot, hot_signals = virality.score_text(
        "This is the most expensive card ever. Logan Paul paid $400,000 for it.",
        celebrity_names=("Logan Paul",),
    )
    bland, bland_signals = virality.score_text("We drove to the store and parked the car.")

    assert hot > bland
    assert bland == 0.0 and bland_signals == ()
    names = {signal.name for signal in hot_signals}
    assert {"big_money", "superlative", "celebrity"} <= names


def test_score_text_caps_repeated_signal() -> None:
    spam = "$5 million " * 9
    score, signals = virality.score_text(spam)
    money = next(signal for signal in signals if signal.name == "big_money")
    # Each $5M match is base 1 + magnitude bonus 4 = 5; nine of them = 45,
    # held down to the money cap of 10.
    assert money.weight == 10.0
    assert score == 10.0


def test_money_magnitude_outweighs_trivial_amount() -> None:
    big, _ = virality.score_text("They sold it for $400,000.")
    small, _ = virality.score_text("I bought a bottle for $100.")
    assert big > small


def test_reasons_are_human_readable() -> None:
    _, signals = virality.score_text("$16,492,000 sale", celebrity_names=())
    moment = virality.MomentCandidate(
        start=0.0, end=10.0, text="$16,492,000 sale",
        signal_score=3.0, duration_fit=1.0, signals=signals,
    )
    assert any("big money" in line for line in moment.reasons)


def test_duration_fit_favors_short_punchy_clips() -> None:
    assert virality.duration_fit(12.0) == 1.0  # just above the floor -> ideal
    assert virality.duration_fit(12.0) > virality.duration_fit(40.0)  # short beats rambly
    assert virality.duration_fit(5.0) == 0.0  # below floor -> unusable
    # The floor is campaign-driven: the same 12s clip fails a 15s-min campaign.
    assert virality.duration_fit(12.0, min_seconds=15.0) == 0.0


def test_history_signal_rewards_origin_framing() -> None:
    score, signals = virality.score_text(
        "This predates everything — the origin of the TCG, back in 1996."
    )
    assert score > 0
    assert any(signal.name == "history" for signal in signals)


def test_final_score_folds_in_duration() -> None:
    _, signals = virality.score_text("$400,000 sale", celebrity_names=())
    tight = virality.MomentCandidate(0.0, 20.0, "$400,000 sale", 10.0, 1.0, signals)
    rambly = virality.MomentCandidate(0.0, 42.0, "$400,000 sale", 10.0, 0.72, signals)
    # Same signals, longer clip ranks lower once length is folded in.
    assert tight.score > rambly.score
    assert tight.length_note == "ideal" and rambly.length_note == "long"


_RANK_SRT = """\
1
00:00:00,000 --> 00:00:03,000
Just some ordinary chit chat about nothing much here.

2
00:00:03,000 --> 00:00:06,000
We talked about the weather and had lunch together.

3
00:00:30,000 --> 00:00:34,000
This is the rarest card ever and it sold for $400,000.

4
00:00:34,000 --> 00:00:38,000
Honestly it was the most insane discovery, nobody knew it existed.
"""


def test_rank_moments_surfaces_the_hot_beat_over_chatter() -> None:
    sentences = tc.sentences_from_srt(_RANK_SRT)
    ranked = virality.rank_moments(sentences, top_n=3, target_seconds=8.0)

    assert ranked, "expected at least one ranked moment"
    top = ranked[0]
    # The money/superlative/curiosity beat at ~30s must outrank the small talk.
    assert top.start >= 28.0
    assert "400,000" in top.text
    assert top.score > 0
    names = {signal.name for moment in ranked for signal in moment.signals}
    assert "big_money" in names


def test_rank_moments_returns_empty_when_no_signals() -> None:
    flat = """\
1
00:00:00,000 --> 00:00:03,000
We walked along the road on a calm afternoon.

2
00:00:03,000 --> 00:00:06,000
Then we sat down and rested for a little while.
"""
    sentences = tc.sentences_from_srt(flat)
    assert virality.rank_moments(sentences) == []
