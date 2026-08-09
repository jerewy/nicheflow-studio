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
    clip_window_around,
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

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)

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


def _overlaps(a: MomentCandidate, b_start: float, b_end: float, *, min_ratio: float) -> bool:
    overlap = min(a.end, b_end) - max(a.start, b_start)
    if overlap <= 0:
        return False
    shorter = min(a.end - a.start, b_end - b_start) or 1.0
    return overlap / shorter >= min_ratio


def rank_moments(
    sentences: list[TranscriptSentence],
    *,
    celebrity_names: tuple[str, ...] = (),
    top_n: int = 10,
    target_seconds: float = 14.0,
    min_seconds: float = DEFAULT_MIN_SECONDS,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    overlap_ratio: float = 0.4,
) -> list[MomentCandidate]:
    """Rank the most clippable moments in a transcript.

    Every sentence with a hook signal seeds a clip window (via
    :func:`clip_window_around`); the window's full text is scored, near-duplicate
    windows are collapsed keeping the strongest, and the top ``top_n`` are
    returned highest-score first.
    """
    scored_windows: list[MomentCandidate] = []
    seen_spans: set[tuple[float, float]] = set()

    for sentence in sentences:
        seed_score, _ = score_text(sentence.text, celebrity_names=celebrity_names)
        if seed_score <= 0:
            continue
        window: ClipWindow = clip_window_around(
            sentences,
            (sentence.start + sentence.end) / 2,
            target_seconds=target_seconds,
            min_seconds=min_seconds,
            max_seconds=max_seconds,
        )
        window = _extend_to_payoff(
            sentences, window, max_seconds=max_seconds, celebrity_names=celebrity_names
        )
        span = (round(window.start, 3), round(window.end, 3))
        if span in seen_spans:
            continue
        seen_spans.add(span)
        window_score, signals = score_text(window.text, celebrity_names=celebrity_names)
        if window_score <= 0:
            continue
        scored_windows.append(
            MomentCandidate(
                start=window.start,
                end=window.end,
                text=window.text,
                signal_score=window_score,
                duration_fit=duration_fit(window.duration, min_seconds=min_seconds),
                signals=signals,
            )
        )

    scored_windows.sort(key=lambda moment: moment.score, reverse=True)

    ranked: list[MomentCandidate] = []
    for candidate in scored_windows:
        if any(
            _overlaps(accepted, candidate.start, candidate.end, min_ratio=overlap_ratio)
            for accepted in ranked
        ):
            continue
        ranked.append(candidate)
        if len(ranked) >= top_n:
            break
    return ranked
