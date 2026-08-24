"""Rank long-form transcript moments by their potential to pop as a short clip.

This is the analysis layer that sits *before* clipping: feed it the sentences a
long video was split into (see :mod:`transcript_clips`) and it surfaces the
moments most likely to go viral, each with a timestamp, the transcript context,
and an explained score — so a human can do the final quality control and pick.

It is deliberately heuristic, not ML: transparent keyword/pattern signals with
tunable weights. Every score comes with the exact phrases that fired it, which
is what makes it useful for review rather than a black box. No video, network,
or extra dependency is required.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from nicheflow_studio.processing.transcript_clips import (
    DEFAULT_MAX_SECONDS,
    DEFAULT_MIN_SECONDS,
    DEFAULT_TARGET_SECONDS,
    ClipWindow,
    TranscriptSentence,
    clip_window_from,
    ends_mid_thought,
    opens_mid_thought,
)


@dataclass(frozen=True)
class ViralitySignal:
    """One category of hook signal that fired, with the phrases that matched."""

    name: str
    weight: float
    matches: tuple[str, ...]


# Shorter clips retain (and therefore earn) better on short-form — the ideal is
# a punchy cut just above the campaign's minimum duration, not a long one. The
# fit multiplier rewards the short band and tapers off as a clip drags on.
# ``min_seconds`` is campaign-driven (this campaign's floor is "over 7s").
def duration_fit(seconds: float, *, min_seconds: float = DEFAULT_MIN_SECONDS) -> float:
    """Multiplier in [0, 1] rewarding short, punchy clips above the floor."""
    if seconds < min_seconds:
        return 0.0  # below the campaign floor -> not usable
    over = seconds - min_seconds
    if over <= 8:  # punchy: within ~8s of the floor
        return 1.0
    if over <= 15:
        return 0.9
    if over <= 25:
        return 0.78
    if over <= 38:
        return 0.66
    return 0.55


@dataclass(frozen=True)
class MomentCandidate:
    """A ranked clippable moment: where it is, what it says, and why it ranks."""

    start: float
    end: float
    text: str
    signal_score: float
    duration_fit: float
    signals: tuple[ViralitySignal, ...]
    # Seconds shaved off the front because the window opened mid-thought, and
    # whether it *still* does after that. Both are review aids, not filters: a
    # clip that opens on "And I'm like…" is worth cutting, just not worth
    # trusting the in-point of.
    opening_trimmed: float = 0.0
    opens_mid_thought: bool = False
    # And the same for the out-point: true when the window still stops before the
    # speaker finishes, after the close-extension had its go. Usually means the
    # transcript ran out of room inside max_seconds.
    ends_mid_thought: bool = False
    # Silence kept after the final word so the clip has a beat to land on.
    # Included in ``end`` (it is part of the cut) but excluded from the scoring
    # duration — holding on a reaction is not the same as saying more.
    tail_hold: float = 0.0

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)

    @property
    def spoken_duration(self) -> float:
        """The talking part, without the held beat — what the score is judged on."""
        return round(self.end - self.start - self.tail_hold, 3)

    @property
    def score(self) -> float:
        """Final ranking score: signal strength adjusted for clip length."""
        return round(self.signal_score * self.duration_fit, 2)

    @property
    def length_note(self) -> str:
        if self.duration < DEFAULT_MIN_SECONDS:
            return "short"
        return "ideal" if self.duration_fit >= 0.9 else "long"

    @property
    def reasons(self) -> list[str]:
        """Human-readable 'why this might pop' lines for review."""
        lines: list[str] = []
        for signal in self.signals:
            sample = ", ".join(dict.fromkeys(signal.matches))
            lines.append(f"{signal.name.replace('_', ' ')}: {sample}")
        return lines


# Each signal is (name, weight-per-match, cap, compiled pattern). The cap keeps a
# moment from scoring high on sheer repetition of one signal. Weights are tuned
# so a genuinely shareable beat (big money + a superlative, or a named creator)
# outranks generic chatter — adjust freely, nothing downstream hardcodes them.
_MONEY_RE = re.compile(
    r"\$\s?\d[\d,\.]*(?:\s?(?:million|billion|thousand|grand|k))?"
    r"|\b\d[\d,\.]*\s?(?:million|billion|thousand|grand|k)\b"
    r"|\b(?:quarter|half)\s+(?:a\s+)?million\b",
    re.IGNORECASE,
)
_SUPERLATIVE_RE = re.compile(
    r"\b(?:most|best|first ever|the first|only one|the only|rarest|biggest|largest|"
    r"highest|greatest|holy grail|one of one|never seen|never been|record|world'?s)\b",
    re.IGNORECASE,
)
_CURIOSITY_RE = re.compile(
    r"\b(?:discover(?:ed|y)?|found|nobody knew|no one knew|no one is talking|secret|"
    r"turns out|you won'?t believe|what if|hidden|unknown|mystery|out of nowhere)\b",
    re.IGNORECASE,
)
_DRAMA_RE = re.compile(
    r"\b(?:scam|fraud|fake|scandal|controvers\w*|stole|stolen|lawsuit|sued|banned|"
    r"exposed|caught|deep ?fake)\b",
    re.IGNORECASE,
)
_EMOTION_RE = re.compile(
    r"\b(?:oh my god|no way|insane|crazy|unbelievable|mind-?blow\w*|shocked|holy|nuts)\b",
    re.IGNORECASE,
)
# Origin / history framing — the account's proven lane ("the first Pokémon card").
# Deliberately excludes bare "first" (already caught by superlative) to focus on
# date and origin language: 1996, the birth of, predates, how it all started.
_HISTORY_RE = re.compile(
    r"\b(?:origin|originated|the birth of|birthplace|invented|back in (?:19|20)\d\d"
    r"|in (?:19|20)\d\d|history of|the story of|how it (?:all )?(?:started|began)"
    r"|earliest|predates|first appearance|before anyone (?:knew|else))\b",
    re.IGNORECASE,
)

_SIGNAL_SPECS: tuple[tuple[str, float, float, re.Pattern[str]], ...] = (
    ("superlative", 2.0, 6.0, _SUPERLATIVE_RE),
    ("history", 2.0, 6.0, _HISTORY_RE),
    ("curiosity", 1.5, 4.5, _CURIOSITY_RE),
    ("drama", 2.5, 7.5, _DRAMA_RE),
    ("emotion", 1.0, 3.0, _EMOTION_RE),
)

_CELEBRITY_WEIGHT = 4.0
_CELEBRITY_CAP = 12.0

# Money is scored separately from the flat signals so magnitude matters: a
# six-figure number is a far stronger hook than "$100". Each mention earns a
# base point plus a magnitude bonus, capped so one number-heavy line can't
# dominate the whole ranking.
_MONEY_BASE = 1.0
_MONEY_CAP = 10.0


def _money_value(match_text: str) -> float:
    """Approximate the dollar value of a matched money phrase."""
    lowered = match_text.lower()
    if "quarter" in lowered:
        return 250_000.0 if "million" in lowered else 0.25
    if "half" in lowered:
        return 500_000.0 if "million" in lowered else 0.5
    cleaned = lowered.replace("$", "").replace(",", "")
    multiplier = 1.0
    if "billion" in cleaned:
        multiplier = 1e9
    elif "million" in cleaned:
        multiplier = 1e6
    elif "thousand" in cleaned or "grand" in cleaned:
        multiplier = 1e3
    elif re.search(r"\d\s*k\b", cleaned):
        multiplier = 1e3
    number = re.search(r"\d+(?:\.\d+)?", cleaned)
    return (float(number.group()) if number else 0.0) * multiplier


def _money_bonus(value: float) -> float:
    if value >= 1_000_000:
        return 4.0
    if value >= 100_000:
        return 3.0
    if value >= 10_000:
        return 1.5
    if value >= 1_000:
        return 0.5
    return 0.0


def _score_money(text: str) -> ViralitySignal | None:
    matches = tuple(match.group(0).strip() for match in _MONEY_RE.finditer(text))
    if not matches:
        return None
    total = sum(_MONEY_BASE + _money_bonus(_money_value(match)) for match in matches)
    return ViralitySignal(name="big_money", weight=round(min(total, _MONEY_CAP), 2), matches=matches)


def score_text(
    text: str, *, celebrity_names: tuple[str, ...] = ()
) -> tuple[float, tuple[ViralitySignal, ...]]:
    """Score a block of transcript text; return ``(score, signals)``.

    ``celebrity_names`` is the campaign-specific roster (e.g. the creators who
    appear in the documentary) — named-person mentions are a strong reach signal.
    """
    signals: list[ViralitySignal] = []
    score = 0.0

    money_signal = _score_money(text)
    if money_signal is not None:
        score += money_signal.weight
        signals.append(money_signal)

    for name, weight, cap, pattern in _SIGNAL_SPECS:
        matches = tuple(match.group(0).strip() for match in pattern.finditer(text))
        if not matches:
            continue
        contribution = min(len(matches) * weight, cap)
        score += contribution
        signals.append(ViralitySignal(name=name, weight=contribution, matches=matches))

    celeb_hits: list[str] = []
    lowered = text.lower()
    for celebrity in celebrity_names:
        if celebrity.lower() in lowered:
            celeb_hits.append(celebrity)
    if celeb_hits:
        contribution = min(len(celeb_hits) * _CELEBRITY_WEIGHT, _CELEBRITY_CAP)
        score += contribution
        signals.append(
            ViralitySignal(name="celebrity", weight=contribution, matches=tuple(celeb_hits))
        )

    return round(score, 2), tuple(signals)


def _extend_to_payoff(
    sentences: list[TranscriptSentence],
    window: ClipWindow,
    *,
    max_seconds: float,
    celebrity_names: tuple[str, ...],
    max_extra_seconds: float = 12.0,
) -> ClipWindow:
    """Grow the window forward across trailing signal-bearing sentences.

    A short target can stop one sentence short of the punchline ("My house was
    $215,000." | "This card is $400,000."). This appends following sentences
    while they still carry a hook signal — capturing the payoff — and stops at
    the first sentence with none, so filler is never pulled in. Bounded by
    ``max_seconds`` overall and ``max_extra_seconds`` past the original end.
    """
    # Index of the last sentence the window already covers. Found by index (not
    # time) because proportional per-cue timing can make the next sentence's
    # start jitter slightly before this one's end.
    last_covered = -1
    for index, sentence in enumerate(sentences):
        if sentence.end <= window.end + 0.05:
            last_covered = index

    end = window.end
    texts = [window.text]
    for sentence in sentences[last_covered + 1 :]:
        if sentence.end - window.start > max_seconds:
            break
        if sentence.end - window.end > max_extra_seconds:
            break
        score, _ = score_text(sentence.text, celebrity_names=celebrity_names)
        if score <= 0:
            break
        texts.append(sentence.text)
        end = sentence.end
    if end <= window.end:
        return window
    return ClipWindow(window.start, end, " ".join(texts))


def _extend_to_close(
    sentences: list[TranscriptSentence],
    window: ClipWindow,
    *,
    max_seconds: float,
    max_extra_seconds: float = 8.0,
) -> ClipWindow:
    """Grow the window forward until the last sentence actually finishes.

    ``_extend_to_payoff`` stops at the first sentence carrying no hook signal,
    and the sentence that *closes* a thought is exactly that: the resolution
    names no money and no superlative, so it scores zero and is left out. The
    clip then ends hanging.

    This runs after it and asks a different question — not "is there more
    signal?" but "has the speaker finished?" — so a signal-free closing line is
    included when it is the one that lands the point. Bounded the same way, and
    a no-op when the window already ends on a full sentence.
    """
    if not ends_mid_thought(window.text):
        return window

    last_covered = -1
    for index, sentence in enumerate(sentences):
        if sentence.end <= window.end + 0.05:
            last_covered = index

    end = window.end
    texts = [window.text]
    for sentence in sentences[last_covered + 1 :]:
        if sentence.end - window.start > max_seconds:
            break
        if sentence.end - window.end > max_extra_seconds:
            break
        texts.append(sentence.text)
        end = sentence.end
        if not ends_mid_thought(sentence.text):
            break
    else:
        # Ran out of transcript without closing; the extra fragments add nothing.
        return window
    if end <= window.end or ends_mid_thought(texts[-1]):
        return window
    return ClipWindow(window.start, end, " ".join(texts))


def _hold_after_last_word(
    sentences: list[TranscriptSentence],
    window: ClipWindow,
    *,
    max_hold_seconds: float,
) -> float:
    """Seconds of silence to keep after the final word, capped by the next line.

    The window ends on the last syllable, so the clip stops the instant the
    sentence resolves — before the reaction, the cutaway, or the shot of the
    thing being talked about. A held beat is standard practice in an edit and
    the difference between a clip that lands and one that snaps shut.

    The cap is what keeps it safe: the hold never reaches the next sentence, so
    it can only ever add silence (or B-roll over silence), never the truncated
    first half of a line nobody asked for. A speaker who barely pauses gets a
    correspondingly short hold, which is the right answer — there is no beat
    there to keep.
    """
    if max_hold_seconds <= 0:
        return 0.0
    for sentence in sentences:
        if sentence.start > window.end + 0.01:
            return round(max(0.0, min(max_hold_seconds, sentence.start - window.end)), 3)
    # Nothing follows: the source's own end bounds this, applied by the caller
    # that knows the media duration.
    return round(max_hold_seconds, 3)


def _trim_weak_opening(
    sentences: list[TranscriptSentence],
    window: ClipWindow,
    *,
    min_seconds: float,
    celebrity_names: tuple[str, ...],
    keep_ratio: float = 0.8,
    max_trim_ratio: float = 0.35,
) -> tuple[ClipWindow, float]:
    """Advance the in-point past opening sentences that say nothing.

    A window opens on the sentence that earned its ranking, but that sentence is
    often the back half of a thought ("And I believe this image came from
    somebody's account…"). Measured on two 85-minute sources, half of the top
    twenty moments opened this way.

    Dropping the line is only safe when the moment survives it nearly intact, so
    three conditions are checked: what remains still clears the campaign floor,
    it retains at least ``keep_ratio`` of the window's signal, and no more than
    ``max_trim_ratio`` of the window's duration comes off. The signal ratio is
    what protects a hook that happens to be phrased as a continuation — "And I'm
    like, that's all I want is 400K" carries the number that made it rank, so
    removing it would fail the test and the moment is merely flagged instead.

    ``max_trim_ratio`` exists because the signal test alone measures the wrong
    thing. Setup sentences name no money and no superlative, so they score zero
    and cost nothing to drop — which let this strip 9.3 seconds off a 20-second
    window and leave "we had around 900k worth of value" with no way to tell
    whose value, or of what. Signal survived; the meaning did not. A duration cap
    is a blunt guard, but it bounds the failure in the one dimension the score
    cannot see.

    Returns the window and how many seconds came off the front.
    """
    covered = [
        sentence
        for sentence in sentences
        if sentence.start >= window.start - 0.05 and sentence.end <= window.end + 0.05
    ]
    if len(covered) < 2:
        return window, 0.0

    full_score, _ = score_text(window.text, celebrity_names=celebrity_names)
    trim_budget = window.duration * max_trim_ratio
    first = 0
    while first < len(covered) - 1:
        if not opens_mid_thought(covered[first].text):
            break
        remaining = covered[first + 1 :]
        if window.end - remaining[0].start < min_seconds:
            break
        if remaining[0].start - window.start > trim_budget:
            break
        remaining_score, _ = score_text(
            " ".join(sentence.text for sentence in remaining), celebrity_names=celebrity_names
        )
        if remaining_score < full_score * keep_ratio:
            break
        first += 1

    if first == 0:
        return window, 0.0
    kept = covered[first:]
    trimmed = ClipWindow(
        kept[0].start, window.end, " ".join(sentence.text for sentence in kept)
    )
    return trimmed, round(trimmed.start - window.start, 3)


def _overlaps(a: MomentCandidate, b_start: float, b_end: float, *, min_ratio: float) -> bool:
    overlap = min(a.end, b_end) - max(a.start, b_start)
    if overlap <= 0:
        return False
    shorter = min(a.end - a.start, b_end - b_start) or 1.0
    return overlap / shorter >= min_ratio


# Words too common to say anything about what a moment is *about*. Kept short on
# purpose: this is a topic-overlap guard, not a search index.
_TOPIC_STOPWORDS = frozenset(
    """
    a an and are as at be been but by can did do does for from get got had has have he
    her him his how i if in into is it its just like me my no not of on one or our out
    said say she so than that the their them then there these they this to too up us
    was we were what when which who will with would you your
    """.split()
)
# Two moments sharing this fraction of their content words are treated as the
# same story. Five clips from one source only pay if they are five *different*
# stories; posting the same $400,000 sale from two timestamps splits its own
# audience and reads as spam to anyone following more than one account.
_TOPIC_OVERLAP_RATIO = 0.5


def _topic_words(text: str) -> frozenset[str]:
    words = re.findall(r"[a-z']{4,}", text.lower())
    return frozenset(word for word in words if word not in _TOPIC_STOPWORDS)


def _same_topic(a: frozenset[str], b: frozenset[str], *, min_ratio: float) -> bool:
    """Whether two moments are about the same thing, by content-word overlap."""
    if not a or not b:
        return False
    shared = len(a & b)
    return shared / min(len(a), len(b)) >= min_ratio


def rank_moments(
    sentences: list[TranscriptSentence],
    *,
    celebrity_names: tuple[str, ...] = (),
    top_n: int = 10,
    target_seconds: float = 14.0,
    min_seconds: float = DEFAULT_MIN_SECONDS,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    topic_overlap_ratio: float = _TOPIC_OVERLAP_RATIO,
    overlap_ratio: float = 0.4,
    tail_hold_seconds: float = 1.5,
) -> list[MomentCandidate]:
    """Rank the most clippable moments in a transcript.

    Every sentence with a hook signal *opens* a clip window (via
    :func:`clip_window_from`); the window's full text is scored, near-duplicate
    windows are collapsed keeping the strongest, and the top ``top_n`` are
    returned highest-score first.

    The window opens on the signal-bearing sentence rather than centring on it,
    so the line that earned the ranking is what a viewer hears first.
    """
    scored_windows: list[MomentCandidate] = []
    seen_spans: set[tuple[float, float]] = set()

    for index, sentence in enumerate(sentences):
        seed_score, _ = score_text(sentence.text, celebrity_names=celebrity_names)
        if seed_score <= 0:
            continue
        window: ClipWindow = clip_window_from(
            sentences,
            index,
            target_seconds=target_seconds,
            min_seconds=min_seconds,
            max_seconds=max_seconds,
        )
        window = _extend_to_payoff(
            sentences, window, max_seconds=max_seconds, celebrity_names=celebrity_names
        )
        # Then close the sentence the payoff hunt stopped inside of. Ordered this
        # way so the closing line is chosen against the furthest the signal
        # reached, not against a window that would grow past it afterwards.
        window = _extend_to_close(sentences, window, max_seconds=max_seconds)
        # After the payoff is secured, not before: growing forward can only add
        # to what the trim decides about the front, and trimming first would let
        # a shorter window pull in different trailing sentences.
        window, opening_trimmed = _trim_weak_opening(
            sentences, window, min_seconds=min_seconds, celebrity_names=celebrity_names
        )
        span = (round(window.start, 3), round(window.end, 3))
        if span in seen_spans:
            continue
        seen_spans.add(span)
        window_score, signals = score_text(window.text, celebrity_names=celebrity_names)
        if window_score <= 0:
            continue
        # Applied last and kept out of duration_fit: a held beat is part of the
        # cut, but scoring it as clip length would penalise exactly the moments
        # that earned a pause. Deduping still keys off the spoken span above.
        hold = _hold_after_last_word(
            sentences, window, max_hold_seconds=tail_hold_seconds
        )
        scored_windows.append(
            MomentCandidate(
                start=window.start,
                end=round(window.end + hold, 3),
                text=window.text,
                signal_score=window_score,
                duration_fit=duration_fit(window.duration, min_seconds=min_seconds),
                signals=signals,
                opening_trimmed=opening_trimmed,
                opens_mid_thought=opens_mid_thought(window.text),
                ends_mid_thought=ends_mid_thought(window.text),
                tail_hold=hold,
            )
        )

    scored_windows.sort(key=lambda moment: moment.score, reverse=True)

    ranked: list[MomentCandidate] = []
    accepted_topics: list[frozenset[str]] = []
    for candidate in scored_windows:
        if any(
            _overlaps(accepted, candidate.start, candidate.end, min_ratio=overlap_ratio)
            for accepted in ranked
        ):
            continue
        # Time overlap alone is not enough: the same story told twenty minutes
        # apart produces two windows that never overlap and still compete for the
        # same viewers.
        topic = _topic_words(candidate.text)
        if any(_same_topic(topic, seen, min_ratio=topic_overlap_ratio) for seen in accepted_topics):
            continue
        ranked.append(candidate)
        accepted_topics.append(topic)
        if len(ranked) >= top_n:
            break
    return ranked
