"""Clip-campaign rules: the caption template and the submission deadline.

Campaign platforms (Clip Money and friends) pay per 1,000 qualifying views but
reject clips on rule violations, so the constraints below are not cosmetic —
they decide whether a post earns anything:

* the caption MUST carry the campaign's required source mention verbatim,
* every on-screen and caption character must be **English only**, and
* the clip must be submitted in the campaign's Discord within one hour of
  posting AND before it reaches 1,000 views.

Encoding them here (rather than in the UI) keeps one source of truth that both
the render path and the review screen can check against.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass, field

# Campaigns approve English on-screen text and reject anything else. The
# operator is Indonesian, so a stray "yang"/"dengan" in a generated caption is a
# realistic failure mode, not a hypothetical one.
_NON_ASCII_RE = re.compile(r"[^\x00-\x7F]")
# Cheap high-precision check: these are common Indonesian function words that
# cannot appear in correct English. A full language classifier would be
# overkill and would flag legitimate loanwords.
_INDONESIAN_MARKERS = frozenset(
    {
        "yang", "dengan", "untuk", "dari", "dan", "ini", "itu", "tidak", "adalah",
        "akan", "sudah", "juga", "bisa", "saya", "kamu", "kita", "mereka", "ada",
        "pada", "atau", "karena", "tapi", "lebih", "banyak", "orang", "tahun",
    }
)
SUBMISSION_WINDOW_MINUTES = 60
VIEW_SUBMISSION_CEILING = 1000


@dataclass(frozen=True)
class Campaign:
    """The rules of one clip campaign."""

    slug: str
    name: str
    # Verbatim string the caption must contain, e.g. "YOUTUBE: CardBound".
    required_mention: str
    hashtags: tuple[str, ...] = ()
    min_clip_seconds: float = 7.0
    max_clips_per_day: int = 10
    # Payout only counts views from these countries.
    country_tiers: tuple[int, ...] = (1,)
    min_views_per_clip: int = 1000
    min_views_for_payout: int = 5000
    notes: tuple[str, ...] = field(default_factory=tuple)


class CaptionRuleError(ValueError):
    """Raised when a caption would violate the campaign's rules."""


def _non_english_reasons(text: str) -> list[str]:
    reasons: list[str] = []
    # Normalize first so accented forms are reported as the character they are,
    # not as a decomposed sequence.
    stray = sorted({match for match in _NON_ASCII_RE.findall(unicodedata.normalize("NFC", text))})
    if stray:
        reasons.append(f"non-English characters: {' '.join(stray)}")
    words = {word.lower() for word in re.findall(r"[A-Za-z]+", text)}
    indonesian = sorted(words & _INDONESIAN_MARKERS)
    if indonesian:
        reasons.append(f"Indonesian words: {', '.join(indonesian)}")
    return reasons


def check_english_only(text: str) -> list[str]:
    """Reasons ``text`` fails the English-only rule; empty when it passes."""
    return _non_english_reasons(text)


def build_caption(campaign: Campaign, hook: str) -> str:
    """Compose a compliant caption: the hook, the required mention, hashtags.

    The mention and hashtags are appended by the template rather than left to
    the writer, so a campaign clip cannot ship without them.
    """
    cleaned_hook = " ".join(hook.split())
    problems = _non_english_reasons(cleaned_hook)
    if problems:
        raise CaptionRuleError(
            f"Hook fails the campaign's English-only rule ({'; '.join(problems)})."
        )
    parts = [cleaned_hook] if cleaned_hook else []
    parts.append(campaign.required_mention)
    if campaign.hashtags:
        parts.append(" ".join(campaign.hashtags))
    return "\n\n".join(parts)


def validate_caption(campaign: Campaign, caption: str) -> list[str]:
    """Reasons ``caption`` would be rejected; empty list means it is compliant."""
    problems = _non_english_reasons(caption)
    if campaign.required_mention.lower() not in caption.lower():
        problems.append(f"missing required mention: {campaign.required_mention!r}")
    return problems


# --- Persistence ------------------------------------------------------------ #
#
# Campaigns come and go with each drop, so they live in the UI settings store
# rather than the schema — adding one is a user action, not a migration.

_SETTINGS_KEY = "clip_campaigns"
_FIELDS = (
    "slug", "name", "required_mention", "hashtags", "min_clip_seconds",
    "max_clips_per_day", "country_tiers", "min_views_per_clip",
    "min_views_for_payout", "notes",
)


def _to_dict(campaign: Campaign) -> dict:
    return {
        "slug": campaign.slug,
        "name": campaign.name,
        "required_mention": campaign.required_mention,
        "hashtags": list(campaign.hashtags),
        "min_clip_seconds": campaign.min_clip_seconds,
        "max_clips_per_day": campaign.max_clips_per_day,
        "country_tiers": list(campaign.country_tiers),
        "min_views_per_clip": campaign.min_views_per_clip,
        "min_views_for_payout": campaign.min_views_for_payout,
        "notes": list(campaign.notes),
    }


def _from_dict(raw: dict) -> Campaign:
    return Campaign(
        slug=str(raw["slug"]).strip(),
        name=str(raw.get("name") or raw["slug"]).strip(),
        required_mention=str(raw.get("required_mention") or "").strip(),
        hashtags=tuple(str(tag) for tag in raw.get("hashtags") or ()),
        min_clip_seconds=float(raw.get("min_clip_seconds") or 7.0),
        max_clips_per_day=int(raw.get("max_clips_per_day") or 10),
        country_tiers=tuple(int(tier) for tier in raw.get("country_tiers") or (1,)),
        min_views_per_clip=int(raw.get("min_views_per_clip") or 1000),
        min_views_for_payout=int(raw.get("min_views_for_payout") or 5000),
        notes=tuple(str(note) for note in raw.get("notes") or ()),
    )


def list_campaigns() -> list[dict]:
    from nicheflow_studio.services.ui_settings import get_setting

    stored = get_setting(_SETTINGS_KEY, [])
    if not isinstance(stored, list):
        return []
    return [_to_dict(_from_dict(entry)) for entry in stored if isinstance(entry, dict)]


def get_campaign(slug: str) -> Campaign:
    for entry in list_campaigns():
        if entry["slug"] == slug:
            return _from_dict(entry)
    raise CaptionRuleError(f"No campaign named {slug!r}.")


def save_campaign(payload: dict) -> dict:
    """Create or replace a campaign; returns the stored record."""
    from nicheflow_studio.services.ui_settings import set_setting

    if not str(payload.get("slug") or "").strip():
        raise CaptionRuleError("A campaign needs a slug.")
    if not str(payload.get("required_mention") or "").strip():
        raise CaptionRuleError("A campaign needs the required source mention.")
    campaign = _from_dict(payload)
    existing = [entry for entry in list_campaigns() if entry["slug"] != campaign.slug]
    record = _to_dict(campaign)
    set_setting(_SETTINGS_KEY, [*existing, record])
    return record


def delete_campaign(slug: str) -> dict:
    from nicheflow_studio.services.ui_settings import set_setting

    remaining = [entry for entry in list_campaigns() if entry["slug"] != slug]
    set_setting(_SETTINGS_KEY, remaining)
    return {"slug": slug, "remaining": len(remaining)}


def submission_deadline(posted_at: dt.datetime) -> dt.datetime:
    """When the Discord submission window closes for a clip posted at ``posted_at``."""
    return posted_at + dt.timedelta(minutes=SUBMISSION_WINDOW_MINUTES)


def submission_status(posted_at: dt.datetime, now: dt.datetime) -> dict:
    """Countdown state for the review screen's submission reminder.

    The view ceiling is the other half of the rule and cannot be derived from a
    clock, so the caller surfaces it as a warning alongside this.
    """
    deadline = submission_deadline(posted_at)
    remaining = (deadline - now).total_seconds()
    return {
        "deadline": deadline.isoformat(),
        "seconds_remaining": max(0, int(remaining)),
        "expired": remaining <= 0,
        "view_ceiling": VIEW_SUBMISSION_CEILING,
    }
