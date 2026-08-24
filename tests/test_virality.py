from __future__ import annotations

import pytest

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


_HOOK_FIRST_SRT = """\
1
00:00:00,000 --> 00:00:05,000
I think he is a very standup guy and people are generally fine with him.

2
00:00:05,000 --> 00:00:11,000
I just sold him a card for $400,000 and it was the most insane deal ever.

3
00:00:11,000 --> 00:00:17,000
He wired the money the same afternoon without asking a single question.
"""


def test_rank_moments_opens_on_the_hook_not_the_filler_before_it() -> None:
    """The clip must start where the money line starts, not 5s of preamble earlier.

    Short-form is decided in the first second, so a window centred on its own
    hook opens on "I think he is a very standup guy" and wastes the only second
    that matters.
    """
    sentences = tc.sentences_from_srt(_HOOK_FIRST_SRT)
    ranked = virality.rank_moments(sentences, top_n=3, target_seconds=10.0, min_seconds=5.0)

    assert ranked, "expected the money line to rank"
    top = ranked[0]
    assert top.start == 5.0, "the window must open on the signal-bearing sentence"
    assert "standup guy" not in top.text, "the preceding filler must not be pulled in"
    assert "$400,000" in top.text
    # It still grows forward into the payoff rather than stopping at the hook.
    assert "wired the money" in top.text


_SAME_STORY_SRT = """\
1
00:00:00,000 --> 00:00:06,000
I sold that Charizard card to him for $400,000 in the end.

2
00:00:06,000 --> 00:00:12,000
It was the most insane sale of my entire collecting career.

3
00:01:00,000 --> 00:01:06,000
I sold that Charizard card for $400,000 which was insane.

4
00:01:06,000 --> 00:01:12,000
The most unbelievable Charizard sale anyone had seen.
"""


def test_rank_moments_drops_the_same_story_told_twice() -> None:
    """Two tellings of one sale never overlap in time but are still one clip.

    Posting both splits their own audience and reads as spam across a network,
    so five slots must mean five different stories.
    """
    sentences = tc.sentences_from_srt(_SAME_STORY_SRT)
    ranked = virality.rank_moments(
        sentences, top_n=5, target_seconds=10.0, min_seconds=5.0
    )
    assert len(ranked) == 1


def test_rank_moments_keeps_both_when_topic_dedup_is_disabled() -> None:
    """Guards the test above: without the topic guard both windows survive."""
    sentences = tc.sentences_from_srt(_SAME_STORY_SRT)
    ranked = virality.rank_moments(
        sentences,
        top_n=5,
        target_seconds=10.0,
        min_seconds=5.0,
        topic_overlap_ratio=2.0,  # unreachable ratio -> guard never fires
    )
    assert len(ranked) == 2


def test_rank_moments_keeps_genuinely_different_stories() -> None:
    different = """\
1
00:00:00,000 --> 00:00:06,000
I sold that Charizard card to him for $400,000 in the end.

2
00:01:00,000 --> 00:01:06,000
The grading scandal exposed hundreds of counterfeit Pikachu submissions.
"""
    sentences = tc.sentences_from_srt(different)
    ranked = virality.rank_moments(
        sentences, top_n=5, target_seconds=6.0, min_seconds=5.0
    )
    assert len(ranked) == 2


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


def test_rank_moments_trims_a_dangling_opening_line() -> None:
    # The window opens on a joining sentence that carries only a fraction of the
    # signal; the hook itself lands in the two sentences after it.
    srt = """\
1
00:00:00,000 --> 00:00:06,000
And that was the scandal everyone remembers.

2
00:00:06,000 --> 00:00:12,000
The buyer paid $400,000 for the card in cash.

3
00:00:12,000 --> 00:00:18,000
It was the most expensive sale ever, and the seller was exposed as a fraud.
"""
    sentences = tc.sentences_from_srt(srt)
    moment = virality.rank_moments(sentences, top_n=1)[0]

    assert moment.opening_trimmed > 0
    assert moment.start == 6.0
    assert moment.text.startswith("The buyer paid")
    assert moment.opens_mid_thought is False


def test_rank_moments_keeps_a_dangling_opening_that_carries_the_hook() -> None:
    # Same shape, except the number is IN the joining sentence. Trimming would
    # throw away the reason the moment ranked, so it is flagged, not cut.
    srt = """\
1
00:00:00,000 --> 00:00:06,000
And I'm like, that's all I want is $400,000.

2
00:00:06,000 --> 00:00:16,000
We drove home and talked about the weather.
"""
    sentences = tc.sentences_from_srt(srt)
    moment = virality.rank_moments(sentences, top_n=1)[0]

    assert moment.opening_trimmed == 0.0
    assert moment.start == 0.0
    assert moment.opens_mid_thought is True


def test_rank_moments_will_not_trim_below_the_campaign_floor() -> None:
    srt = """\
1
00:00:00,000 --> 00:00:04,000
And that was the scandal everyone remembers.

2
00:00:04,000 --> 00:00:11,000
The buyer paid $400,000 for the card.
"""
    sentences = tc.sentences_from_srt(srt)
    moment = virality.rank_moments(sentences, top_n=1, min_seconds=8.0)[0]

    # Dropping the opener would leave a 7s clip, under the 8s floor.
    assert moment.opening_trimmed == 0.0
    assert moment.opens_mid_thought is True


# --- Clip endings: the resolution carries no keywords, so it scored zero ----- #


def test_ends_mid_thought_reads_terminal_punctuation() -> None:
    assert tc.ends_mid_thought("Logan said, \"You know, John, a smart man told me if") is True
    assert tc.ends_mid_thought("This card is $400,000.") is False
    assert tc.ends_mid_thought("Is that really the most expensive one?") is False
    assert tc.ends_mid_thought('He called it "the biggest sale ever."') is False


def test_rank_moments_closes_a_sentence_the_payoff_hunt_stopped_inside() -> None:
    # The closing line names no money and no superlative, so score_text rates it
    # zero and _extend_to_payoff stops before it — leaving the clip hanging on a
    # segment that never finishes. _extend_to_close is what lands the point.
    srt = """\
1
00:00:00,000 --> 00:00:09,000
The buyer paid $400,000 for the single most expensive card ever sold

2
00:00:09,000 --> 00:00:14,000
and he never once looked back.
"""
    sentences = tc.sentences_from_srt(srt)
    # tail_hold off: this is about which sentence the window closes on, and a
    # held beat would move `end` past it for a reason unrelated to the question.
    moment = virality.rank_moments(sentences, top_n=1, tail_hold_seconds=0.0)[0]

    assert moment.end == 14.0
    assert moment.text.endswith("never once looked back.")
    assert moment.ends_mid_thought is False


def test_rank_moments_flags_an_ending_it_could_not_close() -> None:
    # Nothing left in the transcript to close on, so the flag is the only honest
    # output — the operator nudges the out-point in the Trim step.
    srt = """\
1
00:00:00,000 --> 00:00:12,000
The buyer paid $400,000 for the most expensive card ever and then he
"""
    sentences = tc.sentences_from_srt(srt)
    moment = virality.rank_moments(sentences, top_n=1)[0]

    assert moment.ends_mid_thought is True


def test_rank_moments_does_not_extend_a_window_that_already_closes() -> None:
    # Long enough on its own to satisfy target_seconds, so nothing but
    # _extend_to_close could pull the next sentence in — and it must not.
    srt = """\
1
00:00:00,000 --> 00:00:16,000
The buyer paid $400,000 for the most expensive card ever sold.

2
00:00:16,000 --> 00:00:24,000
We drove home and talked about the weather.
"""
    sentences = tc.sentences_from_srt(srt)
    moment = virality.rank_moments(sentences, top_n=1, tail_hold_seconds=0.0)[0]

    assert moment.end == 16.0
    assert "weather" not in moment.text
    assert moment.ends_mid_thought is False


def test_rank_moments_will_not_strip_most_of_the_window_as_dead_opening() -> None:
    # The reported failure: a setup sentence names no money, so it costs nothing
    # in signal terms and the old trim removed it wholesale, leaving "we had
    # around 900k worth of value" with no way to tell whose value, or of what.
    srt = """\
1
00:00:00,000 --> 00:00:10,000
And the scandal at that Ohio grading company ran for about six years by then.

2
00:00:10,000 --> 00:00:16,000
We had around 900k worth of value estimated on current market value.

3
00:00:16,000 --> 00:00:22,000
It was the biggest fraud the hobby had ever seen.
"""
    sentences = tc.sentences_from_srt(srt)
    moment = virality.rank_moments(sentences, top_n=1)[0]

    # 10s off a 22s window is 45% — past the 35% cap, so the setup survives and
    # "900k worth of value" still has something to attach to.
    assert moment.opening_trimmed == 0.0
    assert moment.start == 0.0
    assert "Ohio" in moment.text


# --- The held beat: a clip that stops on the last syllable snaps shut -------- #


def test_rank_moments_holds_a_beat_after_the_final_word() -> None:
    # Eight seconds of silence follow, so the full hold fits.
    srt = """\
1
00:00:00,000 --> 00:00:12,000
The buyer paid $400,000 for the most expensive card ever sold.

2
00:00:20,000 --> 00:00:26,000
Anyway, we drove home.
"""
    sentences = tc.sentences_from_srt(srt)
    moment = virality.rank_moments(sentences, top_n=1, tail_hold_seconds=1.5)[0]

    assert moment.tail_hold == 1.5
    assert moment.end == 13.5
    # The score must not be diluted by the beat — it is a pause, not content.
    assert moment.spoken_duration == 12.0


def test_hold_never_reaches_into_the_next_sentence() -> None:
    # Only 0.4s of silence before the next line, so that is the whole hold: any
    # more would play the truncated front of a sentence nobody asked for. The
    # first line is past target_seconds on its own so the window does not simply
    # grow over the gap.
    srt = """\
1
00:00:00,000 --> 00:00:16,000
The buyer paid $400,000 for the most expensive card ever sold.

2
00:00:16,400 --> 00:00:24,000
Anyway, we drove home.
"""
    sentences = tc.sentences_from_srt(srt)
    moment = virality.rank_moments(sentences, top_n=1, tail_hold_seconds=1.5)[0]

    assert moment.tail_hold == pytest.approx(0.4, abs=0.05)
    assert moment.end == pytest.approx(16.4, abs=0.05)


def test_hold_can_be_switched_off() -> None:
    srt = """\
1
00:00:00,000 --> 00:00:12,000
The buyer paid $400,000 for the most expensive card ever sold.
"""
    sentences = tc.sentences_from_srt(srt)
    moment = virality.rank_moments(sentences, top_n=1, tail_hold_seconds=0.0)[0]

    assert moment.tail_hold == 0.0
    assert moment.end == 12.0
