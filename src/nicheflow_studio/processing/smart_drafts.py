from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from nicheflow_studio.processing.video import sample_video_frame_data_urls


DEFAULT_OLLAMA_MODEL = "qwen2.5:7b"
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"
# Vision: Llama 4 Scout — the best vision model on Groq's free tier.
# (Maverick has more experts but is paid-tier only on Groq, returns 404 for
# free accounts. Override GROQ_VISION_MODEL env var if you upgrade.)
DEFAULT_GROQ_VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
SMART_DRAFT_OPTION_COUNT = 3
SMART_CAPTION_OPTION_COUNT = SMART_DRAFT_OPTION_COUNT
SMART_CAPTION_WORD_TARGET = "70-130"
SMART_HASHTAG_TARGET = "3-5"
# Frames: 5 is the HARD ceiling — Groq's Llama 4 Scout vision model rejects
# any chat completion with more than 5 images ("Too many images provided.
# This model supports up to 5 images"). Going higher silently breaks vision.
# (Other models like Maverick have the same cap but are paid-tier anyway.)
DEFAULT_GROQ_MAX_FRAMES = 5
MAX_GROQ_FRAMES_CAP = 5
DEFAULT_REQUEST_TIMEOUT_SECONDS = 45
DEFAULT_RETRY_COUNT = 2  # one initial + two retries; first retry honors any "try again in Xms" hint
GROQ_CHAT_COMPLETIONS_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_VISION_INPUT_PRICE_PER_1M = 0.11
GROQ_VISION_OUTPUT_PRICE_PER_1M = 0.34
GROQ_WRITER_INPUT_PRICE_PER_1M = 0.59
GROQ_WRITER_OUTPUT_PRICE_PER_1M = 0.79
DEFAULT_GROQ_MONTHLY_BUDGET_USD = 1.0
DEFAULT_GROQ_MONTHLY_VIDEO_CAP = 1_000
DEFAULT_GROQ_DAILY_VIDEO_CAP = 40
DEFAULT_GROQ_BUDGET_WARN_RATIO = 0.8
GROQ_REQUESTS_PER_FULL_VIDEO = 2
# Low-context retry: when vision fails on an item we know is starved for
# textual context (generic source title + no transcript), try one more vision
# call with fewer frames. Smaller prompts have a much better chance of
# clearing Groq's free-tier 30K TPM window on the next attempt.
LOW_CONTEXT_RETRY_FRAME_COUNT = 2

# Initial history-hook few-shot set: the account's REAL top performers from
# IG Insights (2026-06-10 review, ranked by accounts engaged) plus one
# curiosity-gap calibration line in the strongest competitor's voice. WO-6
# replaces this static block with measured per-account winners once post
# metrics are available.
_HISTORY_LOST_ARCHIVE_FEW_SHOT_WINNERS = (
    "Janet Jackson turned grief into a VMA tribute",
    "Princess Diana met Rowan Atkinson before Mr Bean changed everything",
    "The 1996 awards moment that made Michael Jackson look shy",
    "The moment John Denver wrote Annie's Song while riding a ski lift",
    "She performed what would become one of the hardest rap openings ever...",
)


@dataclass(frozen=True)
class SmartDrafts:
    summary: str
    title_options: list[str]
    caption_options: list[str]
    provider_label: str
    recommended_title_index: int | None = None
    recommended_caption_index: int | None = None
    recommendation_reason: str | None = None
    option_notes: list[str] | None = None
    option_tiers: list[str] | None = None
    used_fallback: bool = False
    vision_payload: dict[str, object] | None = None
    generation_meta: dict[str, object] | None = None


class VisionRequiredError(RuntimeError):
    """Raised when require_vision=True and the item could not be vision-grounded.

    Callers (eg. test_generation.py --require-vision) catch this to fail loudly
    instead of accepting writer-only or local-fallback output that would read
    as generic on a low-context clip.
    """


# Generic Instagram source titles that carry no real signal about the clip.
# `Video by <handle>` is what `instagram_scrape_urls.py` writes when the post
# has no caption; treating these as low-context lets us retry vision and/or
# fail fast under --require-vision instead of dressing up generic writer output.
_GENERIC_TITLE_PREFIXES = ("video by ", "reel by ", "post by ")
_LOW_CONTEXT_MIN_USEFUL_CHARS = 12
# Matches filenames/IDs that are just a platform tag plus an opaque token —
# eg. `Instagram_DYfJT5WOtzJ`, `shorts_abc123`, `reel_XYZ-1`. These carry no
# semantic information about the clip and should be treated as low-context.
_ID_LIKE_TITLE_RE = re.compile(
    r"^(instagram|reel|reels|shorts?|short|youtube|yt|tiktok|video)[\s_\-:]+[A-Za-z0-9_\-]+$",
    re.IGNORECASE,
)


def _is_low_context_source_title(
    source_title: str | None,
    transcript_text: str | None,
) -> bool:
    """True when neither the source title nor the transcript gives the model
    enough grounding to write a clip-specific draft on its own.

    Vision becomes the only meaningful signal in this case, so callers can use
    this flag to (a) retry vision with a smaller prompt and (b) refuse to
    return writer-only output under --require-vision.
    """
    transcript = _normalize_whitespace(transcript_text or "")
    if len(transcript) >= 40:
        return False
    title = _normalize_whitespace(source_title or "")
    if not title:
        return True
    lowered = title.casefold()
    if any(lowered.startswith(prefix) for prefix in _GENERIC_TITLE_PREFIXES):
        return True
    if _ID_LIKE_TITLE_RE.match(title):
        return True
    if len(title) < _LOW_CONTEXT_MIN_USEFUL_CHARS:
        return True
    return False


def generate_smart_drafts(
    *,
    transcript_text: str,
    source_title: str | None,
    niche_label: str | None,
    source_description: str | None = None,
    input_path: Path | None = None,
    model: str | None = None,
    api_key: str | None = None,
    account_voice: dict[str, str] | None = None,
    prompt_profile: str | None = None,
    caption_style: str | None = None,
    title_style: str | None = None,
    recent_titles: list[str] | None = None,
    recent_captions: list[str] | None = None,
    few_shot_winners: list[str] | None = None,
    require_vision: bool = False,
) -> SmartDrafts:
    cleaned_transcript = _normalize_whitespace(transcript_text)
    cleaned_source_title = _normalize_whitespace(source_title or "")
    cleaned_source_description = _normalize_whitespace(source_description or "")
    cleaned_niche = _normalize_whitespace(niche_label or "")
    normalized_voice = _normalize_account_voice(account_voice)
    if not any(
        [
            cleaned_transcript,
            cleaned_source_title,
            cleaned_source_description,
            cleaned_niche,
            normalized_voice,
        ]
    ):
        raise RuntimeError("Not enough context to generate smart drafts.")

    low_context = _is_low_context_source_title(
        cleaned_source_title or None,
        cleaned_transcript or None,
    )

    visual_frame_urls: list[str] = []
    if input_path is not None:
        try:
            visual_frame_urls = sample_video_frame_data_urls(
                input_path,
                max_frames=_groq_max_frames(),
            )
        except Exception:
            visual_frame_urls = []

    provider_order = _resolve_provider_order(model=model, api_key=api_key)
    errors: list[str] = []
    for provider, resolved_model, resolved_api_key in provider_order:
        try:
            if provider == "groq":
                result = _generate_groq_smart_drafts(
                    api_key=resolved_api_key or "",
                    reasoning_model=resolved_model,
                    transcript_text=cleaned_transcript,
                    source_title=cleaned_source_title or None,
                    source_description=cleaned_source_description or None,
                    niche_label=cleaned_niche or None,
                    visual_frame_urls=visual_frame_urls,
                    account_voice=normalized_voice,
                    prompt_profile=prompt_profile,
                    caption_style=caption_style,
                    title_style=title_style,
                    recent_titles=recent_titles,
                    recent_captions=recent_captions,
                    few_shot_winners=few_shot_winners,
                    low_context=low_context,
                )
            else:
                result = _generate_ollama_smart_drafts(
                    model=resolved_model,
                    transcript_text=cleaned_transcript,
                    source_title=cleaned_source_title or None,
                    source_description=cleaned_source_description or None,
                    niche_label=cleaned_niche or None,
                    visual_frame_urls=visual_frame_urls,
                    account_voice=normalized_voice,
                    prompt_profile=prompt_profile,
                    caption_style=caption_style,
                    title_style=title_style,
                    recent_titles=recent_titles,
                    recent_captions=recent_captions,
                    few_shot_winners=few_shot_winners,
                    low_context=low_context,
                )
            _enforce_require_vision(result, require_vision=require_vision)
            return result
        except VisionRequiredError:
            # Vision-required failure is a deliberate signal — do not swallow
            # it into the local fallback path, which would mask the issue.
            raise
        except Exception as exc:
            errors.append(str(exc))

    if errors:
        if require_vision and low_context:
            raise VisionRequiredError(
                "Vision required for low-context item but no provider returned "
                "a usable response. Errors: " + " | ".join(errors)
            )
        visual_summary = _summarize_visual_frames_for_local_generation(
            visual_frame_urls=visual_frame_urls,
            source_title=cleaned_source_title or None,
            niche_label=cleaned_niche or None,
        )
        return _generate_local_fallback_drafts(
            transcript_text=cleaned_transcript,
            source_title=cleaned_source_title or None,
            source_description=cleaned_source_description or None,
            niche_label=cleaned_niche or None,
            visual_summary=visual_summary,
            account_voice=normalized_voice,
            prompt_profile=prompt_profile,
            caption_style=caption_style,
            title_style=title_style,
            recent_titles=recent_titles,
            recent_captions=recent_captions,
            errors=errors,
            low_context=low_context,
            frame_count=len(visual_frame_urls),
        )

    raise RuntimeError("Smart draft generation failed: No smart-draft provider is configured.")


def _enforce_require_vision(result: SmartDrafts, *, require_vision: bool) -> None:
    """Raise VisionRequiredError when the caller demanded vision grounding
    for a low-context item but the generation came back without it.

    The signal lives in ``generation_meta`` so callers don't have to
    re-derive it. Only low-context items are gated — clips with a real
    transcript or specific title can stand on writer-only output.
    """
    if not require_vision:
        return
    meta = result.generation_meta or {}
    if not meta.get("low_context"):
        return
    if meta.get("vision_used"):
        return
    vision_error = meta.get("vision_error") or "(no vision_error recorded)"
    raise VisionRequiredError(
        f"require_vision=True: low-context item ran without vision grounding "
        f"(provider={result.provider_label!r}, vision_error={vision_error})."
    )


def can_generate_smart_drafts() -> bool:
    """True if at least one Groq API key is configured (primary or fallback)."""
    return any(
        (os.environ.get(name) or "").strip()
        for name in ("GROQ_API_KEY", "GROQ2_API_KEY", "GROQ3_API_KEY", "GROQ4_API_KEY")
    )


def _generate_ollama_smart_drafts(
    *,
    model: str,
    transcript_text: str,
    source_title: str | None,
    source_description: str | None,
    niche_label: str | None,
    visual_frame_urls: list[str],
    account_voice: dict[str, str],
    prompt_profile: str | None,
    caption_style: str | None,
    recent_titles: list[str] | None,
    recent_captions: list[str] | None,
    few_shot_winners: list[str] | None = None,
    low_context: bool = False,
    title_style: str | None = None,
) -> SmartDrafts:
    visual_payload = _fallback_vision_payload(
        source_title=source_title,
        niche_label=niche_label,
        visual_frame_urls=visual_frame_urls,
    )
    response_payload = _perform_chat_completion_request(
        endpoint=_ollama_chat_endpoint(),
        headers={"Content-Type": "application/json"},
        payload=_build_ollama_payload(
            model=model,
            transcript_text=transcript_text,
            source_title=source_title,
            source_description=source_description,
            niche_label=niche_label,
            vision_payload=visual_payload,
            account_voice=account_voice,
            prompt_profile=prompt_profile,
            caption_style=caption_style,
            title_style=title_style,
            recent_titles=recent_titles,
            recent_captions=recent_captions,
            few_shot_winners=few_shot_winners,
        ),
        provider_name=f"Ollama model {model}",
    )
    parsed = _parse_final_drafts(response_payload, provider_name="Ollama")
    return SmartDrafts(
        summary=parsed.summary,
        title_options=parsed.title_options,
        caption_options=parsed.caption_options,
        provider_label="Ollama Qwen 2.5 7B",
        recommended_title_index=parsed.recommended_title_index,
        recommended_caption_index=parsed.recommended_caption_index,
        recommendation_reason=parsed.recommendation_reason,
        option_notes=parsed.option_notes,
        option_tiers=parsed.option_tiers,
        vision_payload=visual_payload,
        generation_meta={
            "writer_model": model,
            "vision_model": None,
            "frame_count": len(visual_frame_urls),
            "vision_attempted": False,
            # Ollama path never calls a real vision model; the visual_payload
            # is only frame-count metadata, so vision_used stays False to
            # match the Groq path semantics.
            "vision_used": False,
            "vision_retry_attempted": False,
            "vision_error": None,
            "low_context": low_context,
            "caption_style": caption_style,
            "title_style": title_style,
            "recommended_title_option_index": parsed.recommended_title_index,
            "recommended_caption_option_index": parsed.recommended_caption_index,
            "recommendation_reason": parsed.recommendation_reason,
            "option_notes": parsed.option_notes,
            "option_tiers": parsed.option_tiers,
        },
    )


def _generate_groq_smart_drafts(
    *,
    api_key: str,
    reasoning_model: str,
    transcript_text: str,
    source_title: str | None,
    source_description: str | None,
    niche_label: str | None,
    visual_frame_urls: list[str],
    account_voice: dict[str, str],
    prompt_profile: str | None,
    caption_style: str | None,
    recent_titles: list[str] | None,
    recent_captions: list[str] | None,
    few_shot_winners: list[str] | None = None,
    low_context: bool = False,
    title_style: str | None = None,
) -> SmartDrafts:
    vision_model = os.environ.get("GROQ_VISION_MODEL") or DEFAULT_GROQ_VISION_MODEL
    vision_payload: dict[str, object] | None = None
    vision_response: dict[str, object] | None = None
    vision_error: str | None = None
    vision_attempted = False
    vision_retry_attempted = False
    primary_frame_count = len(visual_frame_urls[: _groq_max_frames()])
    if _groq_vision_enabled() and visual_frame_urls:
        vision_attempted = True
        # Vision calls are token-heavy (~13K per call with 5 base64 frames),
        # easily exceeding Groq's free-tier 30K TPM limit on back-to-back calls.
        # Rotate through ALL configured keys (current key first, then the other
        # GROQ*_API_KEY env vars) so a 429 on one key doesn't kill vision —
        # different keys have independent TPM windows.
        vision_payload_template = _build_visual_summary_payload(
            model=vision_model,
            transcript_text=transcript_text,
            source_title=source_title,
            source_description=source_description,
            niche_label=niche_label,
            visual_frame_urls=visual_frame_urls[: _groq_max_frames()],
        )
        vision_keys = _all_groq_keys(preferred_first=api_key)
        for vision_key in vision_keys:
            try:
                vision_response = _perform_chat_completion_request(
                    endpoint=GROQ_CHAT_COMPLETIONS_URL,
                    headers=_groq_headers(vision_key),
                    payload=vision_payload_template,
                    provider_name=f"Groq vision model {vision_model}",
                )
                vision_payload = _parse_vision_payload(vision_response, provider_name="Groq vision")
                vision_error = None
                break  # success — stop rotating
            except RuntimeError as exc:
                vision_error = str(exc)
                vision_payload = None
                vision_response = None
                # On non-rate-limit errors there's no point trying more keys.
                if "429" not in vision_error and "rate_limit" not in vision_error.lower():
                    break

        # Low-context retry: when the item has no transcript and a generic
        # source title, vision is the only real signal. A second pass with
        # 2 frames (rather than 5) cuts the prompt below Groq's 30K TPM
        # window so a borderline 429 has a real chance of clearing.
        if (
            vision_payload is None
            and low_context
            and primary_frame_count > LOW_CONTEXT_RETRY_FRAME_COUNT
        ):
            vision_retry_attempted = True
            retry_template = _build_visual_summary_payload(
                model=vision_model,
                transcript_text=transcript_text,
                source_title=source_title,
                source_description=source_description,
                niche_label=niche_label,
                visual_frame_urls=visual_frame_urls[:LOW_CONTEXT_RETRY_FRAME_COUNT],
            )
            try:
                vision_response = _perform_chat_completion_request(
                    endpoint=GROQ_CHAT_COMPLETIONS_URL,
                    headers=_groq_headers(api_key),
                    payload=retry_template,
                    provider_name=f"Groq vision model {vision_model} (low-context retry)",
                )
                vision_payload = _parse_vision_payload(vision_response, provider_name="Groq vision")
                vision_error = None
            except RuntimeError as exc:
                vision_error = f"{vision_error or 'vision failed'} | retry failed: {exc}"
                vision_payload = None
                vision_response = None

    writer_response = _perform_chat_completion_request(
        endpoint=GROQ_CHAT_COMPLETIONS_URL,
        headers=_groq_headers(api_key),
        payload=_build_groq_payload(
            model=reasoning_model,
            transcript_text=transcript_text,
            source_title=source_title,
            source_description=source_description,
            niche_label=niche_label,
            vision_payload=vision_payload,
            account_voice=account_voice,
            prompt_profile=prompt_profile,
            caption_style=caption_style,
            title_style=title_style,
            recent_titles=recent_titles,
            recent_captions=recent_captions,
            few_shot_winners=few_shot_winners,
        ),
        provider_name=f"Groq reasoning model {reasoning_model}",
    )
    parsed = _parse_final_drafts(writer_response, provider_name="Groq")
    usage_meta = _groq_generation_usage_meta(
        vision_response=vision_response if vision_payload else None,
        writer_response=writer_response,
    )
    return SmartDrafts(
        summary=parsed.summary,
        title_options=parsed.title_options,
        caption_options=parsed.caption_options,
        provider_label="Groq Scout + Llama 3.3" if vision_payload else "Groq Llama 3.3",
        recommended_title_index=parsed.recommended_title_index,
        recommended_caption_index=parsed.recommended_caption_index,
        recommendation_reason=parsed.recommendation_reason,
        option_notes=parsed.option_notes,
        option_tiers=parsed.option_tiers,
        vision_payload=vision_payload,
        generation_meta={
            "writer_model": reasoning_model,
            "vision_model": vision_model if vision_payload else None,
            "frame_count": primary_frame_count,
            "vision_attempted": vision_attempted,
            "vision_used": vision_payload is not None,
            "vision_retry_attempted": vision_retry_attempted,
            "vision_error": vision_error,
            "low_context": low_context,
            # Echo back the user's style choices so the JSON panel makes it
            # obvious what the model actually received — saves a debugging
            # round-trip when output doesn't match the dropdown.
            "caption_style": caption_style,
            "title_style": title_style,
            "recommended_title_option_index": parsed.recommended_title_index,
            "recommended_caption_option_index": parsed.recommended_caption_index,
            "recommendation_reason": parsed.recommendation_reason,
            "option_notes": parsed.option_notes,
            "option_tiers": parsed.option_tiers,
            "limit_profile": _groq_limit_profile(),
            **usage_meta,
        },
    )


def _groq_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "nicheflow-studio/0.1",
    }


def _groq_generation_usage_meta(
    *,
    vision_response: dict[str, object] | None,
    writer_response: dict[str, object],
) -> dict[str, object]:
    vision_usage = _extract_usage(vision_response) if vision_response is not None else None
    writer_usage = _extract_usage(writer_response)
    estimated_cost = 0.0
    if vision_usage is not None:
        estimated_cost += _estimate_usage_cost_usd(
            vision_usage,
            input_price_per_1m=GROQ_VISION_INPUT_PRICE_PER_1M,
            output_price_per_1m=GROQ_VISION_OUTPUT_PRICE_PER_1M,
        )
    if writer_usage is not None:
        estimated_cost += _estimate_usage_cost_usd(
            writer_usage,
            input_price_per_1m=GROQ_WRITER_INPUT_PRICE_PER_1M,
            output_price_per_1m=GROQ_WRITER_OUTPUT_PRICE_PER_1M,
        )
    return {
        "usage": {
            "vision": vision_usage,
            "writer": writer_usage,
        },
        "estimated_cost_usd": round(estimated_cost, 8),
    }


def _extract_usage(response_payload: dict[str, object] | None) -> dict[str, int] | None:
    if response_payload is None:
        return None
    usage = response_payload.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt_tokens = _int_usage_value(usage.get("prompt_tokens"))
    completion_tokens = _int_usage_value(usage.get("completion_tokens"))
    total_tokens = _int_usage_value(usage.get("total_tokens"))
    if prompt_tokens == 0 and completion_tokens == 0 and total_tokens == 0:
        return None
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def _int_usage_value(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    return 0


def _estimate_usage_cost_usd(
    usage: dict[str, int],
    *,
    input_price_per_1m: float,
    output_price_per_1m: float,
) -> float:
    return (
        usage["prompt_tokens"] * input_price_per_1m
        + usage["completion_tokens"] * output_price_per_1m
    ) / 1_000_000


def _smart_draft_prompt(
    *,
    transcript_text: str,
    source_title: str | None,
    niche_label: str | None,
    source_description: str | None = None,
    vision_payload: dict[str, object] | None = None,
    account_voice: dict[str, str] | None = None,
    prompt_profile: str | None = None,
    caption_style: str | None = None,
    title_style: str | None = None,
    recent_titles: list[str] | None = None,
    recent_captions: list[str] | None = None,
    few_shot_winners: list[str] | None = None,
) -> str:
    source_title_text = source_title or "(none)"
    source_description_text = _normalize_whitespace(source_description or "")
    niche_text = niche_label or "(none)"
    transcript_block = transcript_text if transcript_text else "(no transcript available)"
    niche_profile = _niche_profile(niche_label)
    angle_plan = _angle_plan(niche_label)
    _is_meme_niche = any(kw in (niche_text).lower() for kw in ("meme", "comedy", "funny"))
    niche_guidance = (
        f"Write like someone who understands the {niche_text} niche."
        if niche_label
        else "Write in a broadly engaging short-form style without sounding generic."
    )
    transcript_guidance = (
        "Use the transcript as the primary signal when it is present."
        if transcript_text
        else (
            "No-transcript mode: visual evidence and source caption are your primary context. "
            "Do not write generic drafts - anchor to the specific subject, meme format, game, "
            "show, or cultural reference you can identify from those two signals. "
            "If the source caption names a recognisable reference, build the title and caption "
            "around that reference directly."
        )
    )
    grounding_guidance = _grounding_guidance(
        transcript_text=transcript_text,
        vision_payload=vision_payload,
    )
    style = _profile_style_block(prompt_profile)
    tone_guidance = _tone_guidance(account_voice)
    caption_style_line = _caption_style_line(caption_style)
    dedup_guidance = _recent_draft_dedup_prompt(
        recent_titles=recent_titles,
        recent_captions=recent_captions,
    )
    voice_block = _account_voice_prompt(account_voice or {})
    vision_block = json.dumps(
        vision_payload or _empty_vision_payload(),
        ensure_ascii=False,
        sort_keys=True,
    )
    if source_description_text and not transcript_text:
        source_description_block = (
            "Source caption (PRIMARY CONTEXT - no transcript available): "
            f"{source_description_text} "
            "This is your main signal for understanding what this clip is about. "
            "Extract the specific reference - the meme format, game, show, person, or situation - "
            "and build the title and caption directly around it. "
            "Do not copy it verbatim; use it to identify the subject."
        )
    elif source_description_text:
        source_description_block = (
            "Original source caption (supporting context): "
            f"{source_description_text} "
            "Use it to identify the movie, show, game, celebrity, or meme format. "
            "Do not copy it directly; use it as grounding."
        )
    else:
        source_description_block = "Original source caption: (none)"
    return "\n".join(
        [
            "You are a short-form video clipper writing on-screen titles and Instagram "
            "captions for a niche account. Use every signal below to understand the exact "
            "visible moment, then write drafts that feel native to the niche and make "
            "viewers want to follow.",
            "",
            "CLIP CONTEXT",
            f"- Source title: {source_title_text}",
            f"- Account niche: {niche_text}",
            f"- Niche guidance: {niche_guidance}",
            f"- Niche style profile: {niche_profile}",
            f"- {source_description_block}",
            f"- Visual evidence JSON: {vision_block}",
            f"- {voice_block}",
            f"- {dedup_guidance}",
            "",
            "GROUNDING",
            f"- {grounding_guidance}",
            f"- {transcript_guidance}",
            "- Treat the visual evidence JSON as the primary signal for what is on screen. "
            "If a field is empty, do not invent it.",
            "- If on_screen_hook, meme_caption_premise, or implied_premise is present, that "
            "is the strongest seed for the new on-screen title: rebuild the title from that "
            "exact premise in your own words.",
            "- Never invent facts, names, sources, or events that the transcript, title, "
            "niche, source caption, or visual evidence do not support.",
            *_name_the_thing_rules(vision_payload),
            "",
            f"STYLE: {style['style']}",
            "",
            "TONE",
            f"- {tone_guidance}",
            "",
            "ON-SCREEN TITLE",
            # When the user explicitly picks a title_style, suppress the
            # prompt_profile's generic title guidance — otherwise the two
            # compete (the profile's "Use shapes like 'When ...', 'POV: ...'"
            # line never mentions colons, so the model was producing the
            # right SHAPE but ignoring the trailing-colon requirement from
            # title_style. The explicit user choice wins.
            *(
                []
                if _has_specific_title_style(title_style, niche_label, prompt_profile)
                else [f"- {style['title']}"]
            ),
            *effective_title_rules(
                title_style,
                caption_style,
                niche_label,
                prompt_profile,
                few_shot_winners=few_shot_winners,
            ),
            "",
            "HOOK FRAMING (drama is allowed, overclaiming is not)",
            *_hook_drama_and_fact_safety_rules(),
            "",
            "CAPTION",
            f"- Each caption is the Instagram description copy, "
            f"about {_caption_word_target(caption_style)} words. "
            f"Stay inside that range.",
            f"- {style['caption']}",
            "- Separate every paragraph with one blank line. Never return a caption as one "
            "dense block of text.",
            f"- {caption_style_line}",
            "- Start with the hook or the concept itself, never with 'This clip', 'In this "
            "video', 'You need to see', 'The clip shows', 'The video shows', "
            "'This video features', or 'The interview clip shows'.",
            "- Do not use video-description framing anywhere in the caption body: phrases like "
            "'the clip shows X doing Y' or 'in this video, X discusses Y' treat the caption "
            "as a synopsis, not a hook. Write as if talking to a friend about the moment, "
            "not summarising a video for a search engine.",
            "- Never open a caption with a dictionary-style definition: do not write "
            "'[Game/show/thing] is a [category] where...' as the first sentence. "
            "Lead with the feeling, situation, or moment first.",
            f"- End with a final separate line of {_caption_hashtag_target(caption_style)} "
            "specific hashtags. Prefer niche tags over generic spam tags. Do not exceed 5 hashtags.",
            "",
            "OUTPUT OPTIONS",
            f"- title_options: exactly {SMART_DRAFT_OPTION_COUNT} distinct on-screen titles, "
            "each a different angle. Do not write three rewrites of one line.",
            f"- caption_options: exactly {SMART_CAPTION_OPTION_COUNT} distinct captions, each "
            "with a different opening sentence and angle.",
            f"- Option angle plan: {angle_plan}",
            "- final_summary: one short sentence describing what the clip is about.",
            "- recommended_pick: choose the strongest title/caption pair for this specific "
            'account and return {"title_option_index": 1-3, "caption_option_index": 1-3, '
            '"reason": "one short reason"}. Prefer the pair most likely to work for the '
            "account niche, not the cleverest line in isolation.",
            f"- option_notes: exactly {SMART_DRAFT_OPTION_COUNT} short strings, one per option, "
            "labeling each option's use case such as clearest hook, most niche-native, "
            "most curiosity-driven, safest factual pick, or best for reach.",
            f"- option_tiers: exactly {SMART_DRAFT_OPTION_COUNT} strings, one per title in the "
            "same order, each exactly 'green', 'yellow', or 'red' from the HOOK FRAMING "
            "tiers above. 'green' = no checkable claim (safe to auto-post); 'yellow' = "
            "states a concrete fact grounded in the signals; 'red' = an unverifiable "
            "overclaim (you should not have written it — fix the title instead of tagging it red).",
            "",
            "AVOID",
            (
                "- Emojis: a few natural ones are fine for meme content, "
                "but do not force them or use them as filler."
                if _is_meme_niche
                else "- Emojis in captions. Write clean text only."
            ),
            "- Em dashes ('—', '–') and double hyphens ('--') anywhere in titles or "
            "captions. Use a comma, period, or colon instead; long dashes are a tell "
            "of AI-generated copy.",
            "- Video-description phrases in captions: 'the clip shows', 'the video shows', "
            "'this video features', 'in this clip', 'the interview shows'. These read like "
            "YouTube descriptions, not Instagram captions.",
            "- Filler praise and hype words unless the evidence states them: 'master of "
            "rhymes', 'impressive skills', 'must-see', 'caught on camera', 'this clip "
            "showcases'.",
            "- Generic engagement bait such as 'like and follow', 'what do you think', "
            "'drop a comment', or 'tag a friend', unless the account voice explicitly asks "
            "for it.",
            *_anti_explainer_avoid_lines(caption_style, account_voice),
            *_negative_caption_examples_block(caption_style),
            "",
            f"Transcript:\n{transcript_block}",
        ]
    )


def _hook_drama_and_fact_safety_rules() -> list[str]:
    """Permit dramatic/emotional hooks while keeping concrete claims grounded.

    Earlier prompts only said "never invent facts", which the model read as
    "stay neutral" and produced flat, documentary-label titles ("Archival
    footage of X in 1992") that underperform on Reels. This block separates
    the two concerns explicitly: dramatize the MEANING of the moment freely,
    but treat any concrete factual claim as something that must already be in
    the signals. It mirrors the green/yellow/red tiering decided for the
    account voice — see [[nicheflow_ig_profiles]] / [[decisions_nicheflow]].

    Applies across every caption_style: meme styles rarely make factual
    claims, so the rules are a no-op there, while history/cinema/narrative
    styles get the calibration that matters most for them.
    """
    return [
        "- Dramatic, emotional, funny, or curiosity-driven hooks are encouraged. "
        "The title does NOT need to sound like a neutral documentary label such "
        "as 'Archival footage of X in 1992'. Dramatize the MEANING of the "
        "moment, not the facts themselves.",
        "- GREEN (always fine): emotional or interpretive framing of what is "
        "visibly happening, e.g. 'This scene got awkward fast', 'He made it look "
        "effortless', 'This moment still feels unreal'. The clip itself is "
        "enough support; no extra proof needed.",
        "- YELLOW (allowed only when the signals support it): any concrete claim "
        "such as a name, age, height, date, place, record, cause, quote, or a "
        "'first / shortest / biggest' superlative. Use these ONLY when the "
        "transcript, source caption, niche, or visual evidence actually backs "
        "them. If a number, record, or identity is not in the signals, do not "
        "state it as fact.",
        "- RED (never write): unverifiable rarity, secrecy, or world-changing "
        "claims. Banned phrasings include 'never-before-seen', 'rare footage "
        "nobody has seen', 'the last video ever of him', 'they tried to hide "
        "this', 'this changed history forever', 'the rarest footage on the "
        "internet', and 'this destroyed her career'. They assert certainty the "
        "clip cannot back up.",
        "- Mislabeling identity or inventing causation is RED too: never name "
        "the wrong person (e.g. calling another player 'Michael Jordan') and "
        "never invent cause-and-effect ('the routine that made him the greatest "
        "ever'). Frame the meaning instead of fabricating the fact.",
    ]


def _recent_draft_dedup_prompt(
    *,
    recent_titles: list[str] | None,
    recent_captions: list[str] | None,
) -> str:
    title_lines = _dedup_prompt_lines(recent_titles, limit=25, max_chars=120)
    caption_lines = _dedup_prompt_lines(recent_captions, limit=10, max_chars=220)
    if not title_lines and not caption_lines:
        return "Previously used drafts: (none)"

    blocks: list[str] = [
        "Previously used drafts from this niche account. Do not repeat or closely paraphrase these:"
    ]
    if title_lines:
        blocks.append("Titles:\n" + "\n".join(f"- {title}" for title in title_lines))
    if caption_lines:
        blocks.append(
            "Caption openings:\n" + "\n".join(f"- {caption}" for caption in caption_lines)
        )
    blocks.append(
        "Your new title and caption options must be clearly distinct while still "
        "matching the current clip. Distinct means more than reworded: do not "
        "reopen with the same sentence template as a title above, and do not "
        "reuse the same emphasised/bolded keyword that any title above already used."
    )
    return "\n".join(blocks)


def _dedup_prompt_lines(
    values: list[str] | None,
    *,
    limit: int,
    max_chars: int,
) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        cleaned = _normalize_whitespace(str(value))
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        lines.append(cleaned[:max_chars])
        if len(lines) >= limit:
            break
    return lines


def _normalize_caption_style(caption_style: str | None) -> str:
    """Canonicalise the caption_style value, handling legacy aliases.

    Old "relatable" rows in the DB are remapped to "meme_relatable" so the
    rebuilt rules apply automatically without a migration.
    """
    style = _normalize_whitespace(caption_style or "contextual_info").casefold()
    if style == "relatable":
        return "meme_relatable"
    if style in {"context", "info", "context_info", "contextual"}:
        return "contextual_info"
    if style in {"cinema", "cinema_hook", "movie"}:
        return "cinema_hook"
    if style in {
        "archive",
        "history",
        "history_archive",
        "lost_archive",
        "mister_lost_archive",
        "past_moments",
        "past_moments_daily",
    }:
        return "history_lost_archive"
    return style


_MEME_CAMPAIGN_STYLES = {
    "meme_friend_group",
    "meme_bro_main_character",
    "meme_chronically_online",
    "meme_reaction_situation",
    "meme_daily_cope",
}


def _is_meme_campaign_style(caption_style: str | None) -> bool:
    return _normalize_caption_style(caption_style) in _MEME_CAMPAIGN_STYLES


def _caption_word_target(caption_style: str | None) -> str:
    """Return the per-style caption word-count target.

    Each style has a different target because the formats differ wildly:
    - meme_relatable: HYBRID. 40-80 words. Hook line + light context + tags.
    - meme_factual:   50-120 words. meme.ig pattern — emoji + Wikipedia-style
                      context about WHO is in the clip.
    - narrative:      100-200 words. @theanomalists / news-article style —
                      multi-paragraph story about the SPECIFIC MOMENT in the
                      clip (what happened, who's involved, why it matters).
                      Longest of all styles because the value IS the context.
    - hype/default:   70-130 words. Medium grounded caption.
    """
    style = _normalize_caption_style(caption_style)
    if style == "meme_daily_cope":
        return "75-130"
    if style == "meme_relatable" or style in _MEME_CAMPAIGN_STYLES:
        return "40-80"
    if style == "meme_factual":
        return "50-120"
    if style == "narrative":
        return "100-200"
    if style == "news_brief":
        # meme.ig post-1 pattern: short engagement opener + 2-4 fact paragraphs.
        # Total tracks the observed ~85-word post on Emergent Labs.
        return "60-120"
    if style == "cinema_hook":
        # cinema.defined pattern: punchy hook + Wikipedia-style synopsis.
        # Observed captions run 80-160 words.
        return "80-160"
    if style == "history_lost_archive":
        # Past Moments Daily pattern: short history hook + 2 compact context
        # paragraphs. Long enough to add value, shorter than full narrative.
        return "90-150"
    return "70-130"


def _caption_hashtag_target(caption_style: str | None) -> str:
    """Per-style hashtag count target.

    meme.ig data: only 9% of their posts have hashtags. Meme Factual mirrors
    that with 0-2 optional. Meme Relatable (cold-start) needs discoverability
    so it keeps 3-5 hashtags. Narrative (theanomalists pattern) also keeps
    3-5 narrow tags for search reach.
    """
    style = _normalize_caption_style(caption_style)
    if style == "meme_factual":
        return "0-2"
    if style == "news_brief":
        # meme.ig post-1 had zero hashtags. Allow 0-2 so the model can add a
        # single specific tag if the topic is obviously taggable, but the
        # default behaviour matches the observed pattern: no hashtags.
        return "0-2"
    if style == "cinema_hook":
        # cinema.defined rarely uses hashtags — none to 2 niche film tags max.
        return "0-2"
    if style == "history_lost_archive":
        return "3-5"
    return "3-5"


def _caption_paragraph_rule(caption_style: str | None) -> str:
    """Per-style paragraph-structure rule injected into the system prompt."""
    style = _normalize_caption_style(caption_style)
    if style == "meme_daily_cope":
        return (
            "Each caption_options string must follow this DAILY COPE structure: "
            "Line 1 is a relatable cope hook (6-12 words) about pretending to be fine, "
            "overconfidence, exhaustion, denial, losing gracefully, or barely holding it together. "
            "Then a blank line. Then 4-6 short casual sentences across 1-2 paragraphs. "
            "Make it feel like a friend narrating the specific visible moment, with a little "
            "extra context and a clear emotional beat; do not turn it into an essay or explain "
            "the joke academically. Then a blank line and a final line of "
            f"{_caption_hashtag_target(caption_style)} hashtags. Total caption length: 75-130 words."
        )
    if style == "meme_relatable" or style in _MEME_CAMPAIGN_STYLES:
        return (
            "Each caption_options string must follow this HYBRID structure: "
            "Line 1 is a single relatable hook (5-10 words, viewer-centric: "
            "'me when ...', 'when ...', 'pov: ...'). Then a blank line. "
            "Then 2-3 short casual sentences (one short paragraph) giving "
            "light context about what's in the clip — enough text for "
            "Instagram's algorithm to categorize the post for Explore reach, "
            "but still conversational, not encyclopedic. Then a blank line "
            f"and a final line of {_caption_hashtag_target(caption_style)} hashtags. "
            "Total caption length: 40-80 words."
        )
    if style == "meme_factual":
        return (
            "Each caption_options string must follow the meme.ig template: "
            "ONE emoji on the first line (😂 default, 😭 for sad/cute, 🥹 for "
            "emotional, 🤣 for hilarious), then a blank line, then 2-3 short "
            "Wikipedia-style paragraphs of NEUTRAL factual context about the "
            "person/topic in the clip. Do NOT use 'me when' framing. "
            f"Hashtags: {_caption_hashtag_target(caption_style)} (optional)."
        )
    if style == "narrative":
        return (
            "Each caption_options string must follow the @theanomalists "
            "news-article template: a direct narrative opener (no emoji, no "
            "'me when' hook), then 2-4 paragraphs separated by blank lines "
            "telling the FULL STORY of the specific moment in the clip — who "
            "is involved, what happened, when, and why it matters. Use real "
            "names, dates, and specific context. Tone is journalistic and "
            "informative, like a short news article or feature piece. End "
            f"with a blank line and {_caption_hashtag_target(caption_style)} "
            "narrow specific hashtags."
        )
    if style == "news_brief":
        # The meme.ig post-1 (Emergent Labs / Y Combinator) template: short
        # engagement opener + one fact per paragraph + 1-2 semantic topic
        # emojis at the END of each fact paragraph. No "me when" framing.
        return (
            "Each caption_options string must follow the meme.ig NEWS-BRIEF "
            "template exactly: Line 1 is a SHORT engagement opener of 4-10 "
            "words (e.g. 'What do yall think? 🤔', 'Why is this so accurate?', "
            "'Not gonna lie...') — may end with a single thinking/laughing "
            "emoji or no emoji. Then a blank line. Then 2-4 fact paragraphs "
            "separated by blank lines; each paragraph carries ONE specific "
            "fact about the subject (named entities, companies, people, "
            "dates, numbers, places — be concrete). Each fact paragraph "
            "ENDS with 1-2 semantic topic emojis chosen for what the "
            "paragraph is about (💸 funding/money, ⚡ tech/speed, 🧠 AI/brain, "
            "📈 growth, 🎬 film/show, 📺 TV, 🔥 hype, 🏈 sports, 🎤 music). "
            "Emojis must be semantic — never decorative or random. Total "
            "caption length: 60-120 words. Hashtags: 0-2 (optional)."
        )
    if style == "cinema_hook":
        # cinema.defined template: punchy scene reaction + Wikipedia-style synopsis.
        # Observed pattern: 1-line hook with ellipsis beat + ONE emoji, then blank line,
        # then 2-3 neutral factual paragraphs opening with "Film Name (Year), directed by..."
        return (
            "Each caption_options string must follow the cinema.defined template exactly. "
            "LINE 1 (hook): Reference ONE specific physical object, prop, sound, or line of "
            "dialogue from the scene — something tangible the viewer can picture. Use an "
            "ellipsis (...) as a dramatic pause before the payoff clause. "
            "Template: '[Specific scene detail]... and [emotional consequence]. [EMOJI]'. "
            "Examples: "
            "'That ring drop... and suddenly everything makes sense. 💀' "
            "'The color red appearing again... and it hits completely different. 🤯' "
            "'That last line of dialogue... and the whole film reshapes itself. 💀'. "
            "Use exactly ONE emoji at the end — 💀 for shocking twists or deaths, "
            "🤯 for perspective-shifting reveals, 💔 for tragic or heartbreaking moments. "
            "The hook must be 8-14 words total. "
            "Then a blank line. "
            "Then 2-3 short Wikipedia-style paragraphs of NEUTRAL factual context. "
            "HARD RULE: the FIRST SENTENCE of the synopsis MUST follow this exact format: "
            "'[Film Title] ([Year]), directed by [Director], is a [genre] [film/movie/series] "
            "about [brief premise].' "
            "Continue with ONLY the 2-3 cast members or characters relevant to this "
            "scene (never an exhaustive cast list — it reads like padding), the core "
            "plot, and the theme this moment speaks to. End the FINAL paragraph on the "
            "specific moment shown in this clip, not a broad thesis about the whole film. "
            "Tone is encyclopedic — like an IMDb or Letterboxd synopsis. "
            "Do NOT editorialize, do NOT use 'me when' framing, do NOT comment on quality. "
            f"Hashtags: {_caption_hashtag_target(caption_style)} (optional, e.g. #movietwt #letterboxd)."
        )
    if style == "history_lost_archive":
        return (
            "Each caption_options string must follow the PAST MOMENTS DAILY "
            "template exactly: "
            "Paragraph 1 (SPECIFIC HISTORY OPENER, 1 sentence, 8-16 words): make the "
            "clip feel like a notable moment from the past, but name the "
            "specific subject, object, person, scene, or fact immediately. "
            "Do NOT open with generic archive filler such as 'Opening the "
            "archive', 'A forgotten fact', 'Here is the story', 'This is "
            "about', or 'In this clip'. No emoji, no 'me when', no 'you "
            "won't believe', no 'shocking'. "
            "Then a blank line. "
            "Paragraph 2 (CONTEXT, 2-3 sentences): explain who or what is in "
            "the clip, when or where it happened if known, and why the moment "
            "matters. Use real names, dates, shows, places, or events only "
            "when the source data supports them; never invent facts. "
            "Then a blank line. "
            "Paragraph 3 (MOMENT PAYOFF, 1-2 sentences): zoom in on the "
            "detail that makes this file strange, forgotten, darker, funny, "
            "or worth sharing. Explain why the detail matters, why it aged "
            "strangely, or why it became memorable; do not simply restate "
            "the same fact from Paragraph 2. Keep the tone mysterious but "
            "clear, not like a school essay. "
            "Then a blank line and a final line of "
            f"{_caption_hashtag_target(caption_style)} narrow hashtags."
        )
    if style == "contextual_info":
        # Fix A2 enhancement: codify theanomalists' "zoom-in" 3-paragraph arc
        # explicitly. The previous default rule was vague enough that the
        # model only sometimes produced this structure — now it's required.
        return (
            "Each caption_options string must follow the THEANOMALISTS "
            "zoom-in 3-paragraph arc exactly: "
            "Paragraph 1 (HOOK, 1 sentence, 8-16 words): a direct opener that "
            "names the specific moment or sets up the curiosity — no 'me "
            "when' framing, no emoji, no encyclopedia opener. "
            "Then a blank line. "
            "Paragraph 2 (BROADER CONTEXT, 2-3 sentences): the general "
            "pattern, format, show, person, or world this clip lives in. "
            "Use real proper nouns (names, companies, shows, places). This "
            "is where new viewers get oriented — but stay factual, not "
            "encyclopedic. Do NOT define common references the audience "
            "already knows. "
            "Then a blank line. "
            "Paragraph 3 (THIS MOMENT, 1-2 sentences): zoom all the way in "
            "to the specific moment in THIS clip — what actually happened, "
            "who said what, the reaction, the payoff. This is what makes "
            "the post rewatchable. "
            "Then a blank line and a final line of "
            f"{_caption_hashtag_target(caption_style)} mixed hashtags "
            "(one or two generic like #reels + the rest specific to the "
            "subject, show, person, or topic)."
        )
    return (
        "Each caption_options string must contain 2-3 paragraphs separated by "
        f"a blank line, and end with a final line of {_caption_hashtag_target(caption_style)} hashtags."
    )


def _smart_draft_system_prompt(caption_style: str | None) -> str:
    """Build the per-style system prompt with the right word/format rules.

    The previous global "at least 70 words" rule prevented any caption style
    from producing short relatable copy. This function lets each style
    declare its own word range and paragraph structure so the model isn't
    fighting contradictory instructions.
    """
    word_target = _caption_word_target(caption_style)
    paragraph_rule = _caption_paragraph_rule(caption_style)
    return (
        "You create short-form video hooks and captions. "
        "Return only valid JSON with keys final_summary, title_options, caption_options, "
        "recommended_pick, option_notes, option_tiers. "
        f"title_options must contain exactly {SMART_DRAFT_OPTION_COUNT} strings. "
        f"caption_options must contain exactly {SMART_CAPTION_OPTION_COUNT} strings. "
        f"Each caption_options string must be {word_target} words. "
        "recommended_pick must be an object with 1-based title_option_index, "
        "caption_option_index, and a short account-aware reason. "
        f"option_notes must contain exactly {SMART_DRAFT_OPTION_COUNT} short strings explaining each option's angle. "
        f"option_tiers must contain exactly {SMART_DRAFT_OPTION_COUNT} strings, one per title_options "
        "entry, each exactly 'green', 'yellow', or 'red' per the HOOK FRAMING tiers. "
        f"{paragraph_rule}"
    )


# History niche detection — kept in one place so _niche_profile, _angle_plan,
# and the title-rule auto-routing all agree on what counts as "history".
_HISTORY_NICHE_KEYWORDS = (
    "history",
    "historical",
    "vintage",
    "archive",
    "archival",
    "old footage",
    "retro",
)


def _is_history_niche(niche_label: str | None) -> bool:
    niche = _normalize_whitespace(niche_label or "").lower()
    return any(keyword in niche for keyword in _HISTORY_NICHE_KEYWORDS)


# Prompt profiles that carry their OWN deliberate title voice (e.g. story_reel's
# emotional memory hook, cinema's atmospheric line). History auto-routing must
# not override these — it only fills in for the generic/broad profile that the
# archival-footage history accounts (e.g. Past Moments Daily) actually use.
_GENERIC_TITLE_PROFILES = frozenset({"", "broad_short_form"})


def _history_should_auto_route(niche_label: str | None, prompt_profile: str | None) -> bool:
    return (
        _is_history_niche(niche_label)
        and _normalize_whitespace(prompt_profile or "").lower() in _GENERIC_TITLE_PROFILES
    )


def effective_title_rules(
    title_style: str | None,
    caption_style: str | None,
    niche_label: str | None,
    prompt_profile: str | None = None,
    few_shot_winners: list[str] | tuple[str, ...] | None = None,
) -> list[str]:
    """Pick the on-screen title rules, niche-aware.

    Priority:
    1. An explicit title_style the user picked always wins.
    2. Otherwise, a history-niche account on the generic/broad profile defaults
       to the history hook rules (explanatory 9-16 word shapes) instead of the
       generic caption-style fallback — so history hooks land even when no style
       is chosen. A history account on a profile with its own title voice
       (e.g. story_reel) keeps that voice.
    3. Otherwise, fall back to the caption-style-derived title rules.

    Used by BOTH the live generation prompt and the Copy Chat Prompt contract so
    the two never drift.
    """
    explicit = _title_style_rules(title_style, few_shot_winners=few_shot_winners)
    if explicit is not None:
        return explicit
    if _history_should_auto_route(niche_label, prompt_profile):
        return _caption_style_title_rules(
            "history_lost_archive", few_shot_winners=few_shot_winners
        )
    return _caption_style_title_rules(caption_style, few_shot_winners=few_shot_winners)


def _has_specific_title_style(
    title_style: str | None,
    niche_label: str | None,
    prompt_profile: str | None = None,
) -> bool:
    """True when the title rules are a deliberate style (explicit pick OR the
    history auto-route), so the prompt_profile's generic title line should be
    suppressed to avoid two competing title instructions."""
    return _title_style_rules(title_style) is not None or _history_should_auto_route(
        niche_label, prompt_profile
    )


def _title_style_rules(
    title_style: str | None,
    *,
    few_shot_winners: list[str] | tuple[str, ...] | None = None,
) -> list[str] | None:
    """Return on-screen title rules when the caller picked an explicit
    title_style, decoupling title format from caption_style.

    Returns ``None`` to mean "fall back to the caption-style-derived title
    rules" — that's the Auto / None default and preserves all pre-decoupling
    behavior so existing users see zero change unless they touch the new
    Title Style dropdown.

    The new ``meme_setup_punchline`` style is the IGHT/Instagram-meme pattern
    requested via real reference posts: a viewer-centric setup line ending
    in a colon, where the video footage itself delivers the punchline.

    For the other known styles (meme_relatable, meme_factual, narrative,
    contextual_info, news_brief) the rule body is identical to what
    ``_caption_style_title_rules`` would emit — they're exposed here so the
    user can mix any title format with any caption format (e.g. a News
    Brief title with a Context/Info caption).
    """
    if not title_style:
        return None
    style = _normalize_whitespace(title_style).casefold()
    if style in {"auto", "auto_match_caption", "none", ""}:
        return None
    if style in _MEME_CAMPAIGN_STYLES:
        return _meme_campaign_title_rules(style)
    if style == "meme_setup_punchline":
        return [
            "- HARD RULE: each title is a SETUP whose punchline is delivered "
            "by the video footage itself. Trailing colon (:) on the final "
            "line is REQUIRED — it is the cliffhanger signal. Two templates:",
            "- TEMPLATE B (contrast, two lines with a literal '\\n\\n'). "
            "Line 1: what someone says or expects (quoted). "
            "Line 2: the viewer's contradicting reality, ending with a colon. "
            "Total 6-16 words. Common speakers like 'Friend:', 'Group chat:', "
            "'Everyone:' are fine when they are genuinely the funniest fit — "
            "but actively consider more unexpected voices first: 'My GPS:', "
            "'My car insurance:', 'My mom in the passenger seat:', "
            "'My driving instructor:', 'The speed limit sign:', 'Traffic school:'. "
            "An unexpected speaker with a relatable quote beats a generic one.",
            "- OPTION DISTRIBUTION — ALL THREE options MUST use Template B "
            "(two lines with a literal '\\n\\n'). Vary the setup type across them: "
            "Option 1: dialogue contrast — someone says something (quoted), "
            "line 2 is 'Me:' or 'Me when [X]:'. "
            "Example: 'My friends: \\'you\\'re so chill\\' \\n\\n Me:'. "
            "Option 2: situation lead-in — line 1 is a relatable scenario or "
            "question (no quote needed), line 2 is 'Me when [X]:' or 'Me:'. "
            "Example: 'Hey why don\\'t you socialize more \\n\\n Me when I socialize:'. "
            "Option 3: different speaker, different angle — use a completely "
            "different voice and situation from Options 1 and 2. "
            "NEVER return three variations of the same joke.",
            "- CREATIVE REMIX IS REQUIRED: do NOT summarise the clip. "
            "The clip's premise IS the punchline — your job is to invent the "
            "funniest possible setup that makes that punchline land harder. "
            "Ask: what is the most RELATABLE, SPECIFIC, UNEXPECTED situation "
            "that leads to exactly what happens in this clip? That is your title.",
            "- SPECIFICITY RULE: generic setups kill comedy. "
            "Wrong: 'Me when I drive:'. "
            "Right: 'Me driving 40 in a 40 zone and still feeling dangerous:'. "
            "Wrong: 'My friends: \"calm down\" \\n\\n Me:'. "
            "Right: 'My friends: \"it\\'s just a roundabout\" \\n\\n Me:'.",
            "- BANNED: any title that only restates the clip. "
            "BANNED: punchline text in any title line — the video shows it. "
            "BANNED: emoji-only titles. BANNED: hashtags. "
            "BANNED: news-headline form. BANNED: pure noun-phrase labels.",
            "- For Template B, include a literal '\\n\\n' between the two lines "
            "so the title overlay renders them as two visually separated lines.",
        ]
    if style == "cinema_bold_keywords":
        # Same atmospheric cinema title as cinema_hook, but the model also
        # marks the 1-3 highest-impact words with ``**word**`` so the overlay
        # renderer can draw them in a bold weight against the regular text.
        return [
            *_cinema_bold_keyword_mode_rules(),
            "- EMPHASIS MARKUP: wrap the 1-3 single most impactful words in each "
            "title with double asterisks, e.g. '**twist**', '**unravels**', "
            "'**alone**'. These markers are rendered as BOLD on-screen text — they "
            "are formatting, not part of the wording.",
            "- EMPHASIS RULES: mark only 1-3 words total per title (never more); "
            "mark whole words only (never punctuation, never partial words); "
            "never wrap the entire title; pick the words that carry the emotional "
            "punch (the noun or verb the line hinges on), not filler words.",
            "- DISTINCT EMPHASIS (HARD RULE): the bolded word(s) MUST differ across "
            "the three options — never bold the same word in more than one option. "
            "Do NOT default to bolding 'silence', 'moment', or 'connection'; bold "
            "the word each specific title actually hinges on.",
        ]
    # For known caption-driven styles, delegate to the existing rules so
    # users can mix freely without us duplicating rule bodies.
    if style in {
        "meme_relatable",
        "meme_factual",
        "narrative",
        "news_brief",
        "contextual_info",
        "cinema_hook",
        "history_lost_archive",
        *_MEME_CAMPAIGN_STYLES,
    }:
        return _caption_style_title_rules(style, few_shot_winners=few_shot_winners)
    # Unknown title_style: return None so the caller falls back to the
    # caption-derived rules instead of shipping an empty rule list.
    return None


def _cinema_bold_keyword_mode_rules() -> list[str]:
    """Return the non-emphasis rules for Cinema Bold Keywords titles."""
    return [
        "- HARD RULE: generate 3 meaningfully different on-screen title "
        "directions, not 3 rewrites of the same idea. Preferred length is "
        "5-11 words; slightly longer is allowed only when the line reads "
        "naturally and still fits two centered overlay lines.",
        "- BUILD EACH TITLE FROM MIXED INGREDIENTS: combine one concrete "
        "visible anchor from the clip with one broader keyword angle and one "
        "editorial mode. Visible anchors may include a character, face, "
        "object, prop, room, vehicle, light, rain, street, city, doorway, "
        "window, weapon, costume, crowd, color, gesture, final shot, or line "
        "of dialogue. Keyword angles may include atmosphere, mystery, danger, "
        "grief, obsession, beauty, dread, loneliness, revenge, memory, "
        "tension, clue, reveal, corruption, romance, betrayal, survival, "
        "calm, chaos, pressure, scale, detail, or ending.",
        "- TITLE MODES: use 3 different modes across the 3 options. Choose "
        "from visual hook, rewatch detail, watch-if-you-like, mood title, "
        "character title, object/prop title, contrast title, or simple "
        "recommendation title.",
        "- OPTION VARIATION (HARD RULE): across the 3 options, vary the title "
        "mode, opening words, visible anchor, emotional keyword, and sentence "
        "rhythm. At least one option should be direct and plain, at least one "
        "should feel like a rewatch/detail hook, and at least one should feel "
        "like a recommendation or mood hook.",
        "- DO NOT make all titles poetic fragments. DO NOT make all titles "
        "emotional abstractions. Avoid repeated crutches such as 'that kind "
        "of', 'the moment', 'the scene', 'everything changes', 'you never saw "
        "it coming', 'whole story', and 'silence' unless truly specific to "
        "the clip.",
        "- PLAIN LANGUAGE: no film-school jargon. BANNED words include "
        "'score', 'cinematography', 'mise-en-scene', 'diegetic', 'blocking', "
        "'third act', 'framing device', and 'auteur'. Use everyday wording "
        "such as 'music', 'the way it is shot', 'the shots', or 'the ending'.",
        "- BANNED: short meme hooks ('bro thought he had it', 'wait for it'), "
        "'me when', 'POV:', 'that friend who', news-headline form "
        "('Director X Does Y'), titles under 5 words, emoji, and hashtags.",
    ]


def _caption_style_title_rules(
    caption_style: str | None,
    *,
    few_shot_winners: list[str] | tuple[str, ...] | None = None,
) -> list[str]:
    """Return per-style on-screen title rules to inject into the prompt.

    These are extra bullets stacked on top of the base prompt_profile title
    line, so the title format is enforced regardless of which visual
    template the user picked.
    """
    style = _normalize_caption_style(caption_style)
    if style in _MEME_CAMPAIGN_STYLES:
        return _meme_campaign_title_rules(style)
    if style == "history_lost_archive":
        measured_winners = tuple(
            cleaned
            for value in (few_shot_winners or ())
            if (cleaned := _normalize_whitespace(str(value)))
        )
        winners = measured_winners or _HISTORY_LOST_ARCHIVE_FEW_SHOT_WINNERS
        winner_label = (
            "MEASURED ACCOUNT WINNER EXAMPLES"
            if measured_winners
            else "STATIC WINNER EXAMPLES"
        )
        return [
            "- HARD RULE: each title stays concrete about the visible action, era, "
            "and stakes, and pairs them with ONE clear surprise, contrast, emotional "
            "meaning, or historical context. Preferred 10-16 words, acceptable 8-18, "
            "must fit two centered overlay lines. Explain WHY the footage is worth "
            "watching — never just label it. Name the concrete visible subject unless "
            "the single CURIOSITY GAP option deliberately withholds the subject.",
            "- STORY-OPENER RULE: write each title like the first sentence of a "
            "short historical story, not a compressed label or news headline. "
            "Prefer natural story shapes such as 'The [place/event] where [person] "
            "[did the memorable thing]', '[Person] [did the memorable thing] during "
            "[specific situation]', or 'The [ordinary moment] that became [specific "
            "legacy]'. Keep the emotional or situational context when it is the "
            "reason the moment matters.",
            "- COMMENT TEST: a strong title makes the viewer want to reply, share "
            "their take, or tag a friend — intrigue without controversy. Before "
            "returning each title ask: 'would someone stop scrolling and comment "
            "on this?' If it only informs, sharpen the surprise, contrast, or "
            "withheld outcome until it provokes a reaction.",
            "- TWIST BEAT shape (use for at most one option, only when the moment "
            "has a genuine turn): a setup sentence with the concrete subject and "
            "stakes, then a SHORT punch sentence of 2-6 words that lands the "
            "twist. Example: 'After solving a math problem unsolved for 100 "
            "years, Grigori Perelman was offered $1,000,000. He said no.' This "
            "shape may run up to 20 words because the final beat is short. The "
            "punch must state what actually happened — never a teaser like "
            "'what happened next is shocking'.",
            "- CURIOSITY GAP shape (use for EXACTLY one of the three options): "
            "withhold exactly ONE element — the subject ('She...', 'This 12-year-old...') "
            "OR the outcome — while everything else stays concrete (era, action, "
            "stakes). The withheld element must be delivered by the clip in the first "
            "seconds. A trailing ellipsis is allowed. Example: 'She performed what "
            "would become one of the hardest rap openings ever...'.",
            "- COMMENT HOOK shape (AT LEAST one of the three options must use a "
            "question or direct-address form): invite the viewer to weigh in using "
            "concrete action and stakes. It may be the curiosity-gap option only if "
            "it withholds exactly one element. Examples: 'Would you have turned down "
            "the million dollars?', 'Why did her 1990 VMA performance become unforgettable?'.",
            "- OPTION ROTATION (HARD RULE): assign one primary role to each option: "
            "one CURIOSITY GAP, one COMMENT HOOK question/direct-address form, and "
            "one STORY-OPENER. A TWIST BEAT may be used only when it also fills one "
            "of those three roles. Do not return three factual summaries or three "
            "versions of the same shape. Before returning, CHECK the three titles: "
            "if none of them is a question or direct-address line, the response is "
            "INVALID — rewrite one title as the COMMENT HOOK before answering.",
            f"- {winner_label} (calibrate the voice and structure; never copy "
            "unsupported facts into another clip):\n- "
            + "\n- ".join(winners),
            "- Good: 'The ski lift ride where John Denver wrote Annie's Song for "
            "his wife'; 'People actually attached camping tents to scooters in the "
            "1950s'. Too flat: 'John Denver wrote Annie's Song on a ski lift'. "
            "Weak (BANNED — double-withholding hides BOTH subject AND outcome): "
            "'The accessory that disappeared', 'The lost story behind this scene', "
            "'Nobody expected this to matter', 'This old footage aged strangely'. "
            "Withholding one element is allowed only for the single controlled "
            "CURIOSITY GAP option; hiding both is vague mystery bait.",
            "- FACT DISCIPLINE: name an exact year/decade only when provided or "
            "verified; otherwise use 'decades ago' / 'before modern [X]'. Never "
            "invent rarity, disappearance, first-ever status, popularity, "
            "commercial failure, or historical importance.",
            "- BANNED: vague mystery bait ('the lost story', 'aged strangely', "
            "'was not random'); clickbait ('you won't believe', 'shocking', "
            "'changed history forever', 'nobody talks about this'); meme framing "
            "('me when', 'POV:', 'bro', 'send this to'); emoji and hashtags.",
        ]
    if style == "meme_relatable":
        return [
            "- HARD RULE: each title must be 4-8 words and start with one of: "
            "'me ', 'when ', 'that friend who ', 'pov:', 'bro thought ', or "
            "'when X happens'. Make the viewer the subject.",
            "- BANNED: titles that name a celebrity as the subject ('Druski's...', "
            "'Kevin Hart Fail'), generic noun phrases ('Best Moment Of...', "
            "'Funny Clip'), or anything that explains the clip from a distance.",
            "- The title is on-screen text: no hashtags and no emojis.",
        ]
    if style == "meme_factual":
        return [
            "- HARD RULE: each title is a short observational on-screen hook, "
            "3-7 words. Examples that fit the meme.ig style: "
            "'bro thought he had it', 'when she said that', 'lost it instantly', "
            "'this man is wild', 'wait for it', 'caught in 4k'. "
            "Third-person or general observation — describe what's happening "
            "from outside, not from the viewer's perspective.",
            "- BANNED: emoji-only titles (😂 alone is NOT a valid on-screen "
            "title — emojis belong in the caption opener, not the video text). "
            "BANNED: 'me ...', 'when YOU ...', 'pov:', 'that friend who' — "
            "those are Meme Relatable framing, not Meme Factual. "
            "BANNED: fragments under 3 words ('you when', 'lost it'). "
            "BANNED: full sentences with periods.",
            "- The title is on-screen text: no hashtags. No standalone emojis. "
            "A single emoji can appear at the END of a short hook for emphasis "
            "(e.g. 'bro really thought 💀'), but never alone.",
        ]
    if style == "narrative":
        return [
            "- HARD RULE: each title is a long descriptive news-headline-style "
            "phrase, 7-14 words, written in the third person. Tell the reader "
            "WHAT HAPPENS in this specific moment as if writing a magazine "
            "headline. Examples that fit the @theanomalists style: "
            "'The actor dedicates his Golden Globe to his partner's late brother', "
            "'Nobody missed that FIFA knew exactly what it was doing', "
            "'How Lisa made this anthem feel bigger than K-pop', "
            "'This Italian singer created a song that sounded like English'.",
            "- BANNED: 'me ...', 'when YOU ...', 'pov:', 'that friend who' — "
            "those are Meme Relatable framing, not Narrative. "
            "BANNED: short observational fragments ('bro thought he had it') — "
            "those are Meme Factual framing. "
            "BANNED: emoji-only titles. BANNED: titles under 7 words.",
            "- The title is on-screen text: no hashtags, no emojis.",
        ]
    if style == "news_brief":
        # meme.ig post-1 titles: conversational observational hooks, 6-12
        # words, optionally ending in a thinking/laughing emoji. NOT 'me
        # when', NOT POV, NOT news-headline form.
        return [
            "- HARD RULE: each title is a short conversational observation, "
            "6-12 words, written like a real person reacting in chat. "
            "Examples that fit the meme.ig news-brief style: "
            "'Not gonna lie... this is a pretty solid point 🤔', "
            "'Why is this so accurate?', 'Wait... did he just say that?', "
            "'This is actually genius'. "
            "A single thinking/laughing emoji at the END is allowed (🤔 💭 "
            "😅 💀), but never in the middle and never as the whole title.",
            "- BANNED: 'me ...', 'when YOU ...', 'pov:', 'that friend who' — "
            "those belong to Meme Relatable, not News Brief. "
            "BANNED: long news-headline form (7+ words with a clear subject-"
            "verb-object structure) — that's Narrative. "
            "BANNED: encyclopedia or definition openers. "
            "BANNED: emoji-only titles.",
            "- The title is on-screen text: no hashtags.",
        ]
    if style == "cinema_hook":
        # cinema.defined titles: atmospheric hooks about the FEELING of watching
        # the specific movie moment. Earlier this block crowned one template
        # ("That kind of [noun] that...") as DOMINANT and seeded "silence" as an
        # example word — so the model collapsed every generation into the same
        # shape with the same bolded word. We now offer SIX equally-weighted
        # templates and force each of the three options onto a different one.
        return [
            "- HARD RULE: each title is a hook (typically 10-20 words; the "
            "fragment/question templates may be shorter) about the EMOTIONAL "
            "EXPERIENCE of watching this specific movie moment. Pick from these "
            "SIX equally-weighted templates — NONE is default or preferred:",
            "  TEMPLATE A (atmospheric): 'That kind of [noun] that [emotional "
            "consequence].' e.g. 'That kind of ending that quietly rearranges how "
            "you saw the whole story.'",
            "  TEMPLATE B (reveal beat): 'The [moment/scene/line] that [reframes / "
            "changes / breaks] everything — and you never saw it coming.'",
            "  TEMPLATE C (short stab): two or three punchy fragments. e.g. 'One "
            "line. The whole film reshapes. Nobody was ready.'",
            "  TEMPLATE D (before/after contrast): contrast the scale or feeling "
            "before vs after the moment. e.g. 'A whole galaxy of distance, undone "
            "by one quiet thing finally said out loud.'",
            "  TEMPLATE E (second person): put the viewer inside the moment with "
            "'you'. e.g. 'You feel the whole room change the second nobody says a word.'",
            "  TEMPLATE F (question hook): an emotionally specific question. e.g. "
            "'How does one look end up carrying the weight of an entire story?'",
            "- OPTION DISTRIBUTION (HARD RULE): the three options MUST each use a "
            "DIFFERENT template from the list above — never two options on the "
            "same template, never two options that open with the same words. Also "
            "vary the emotional angle: one twist/reveal, one atmosphere/pacing, "
            "one emotional weight.",
            "- VARY THE ANCHOR WORD: do not lean on the same emotional anchor "
            "across options. Rotate clip-grounded nouns (the look, the pause, the "
            "line, the reveal, the music swell, the final shot). BANNED as a "
            "crutch: defaulting titles to 'silence', 'moment', or 'connection' — "
            "use them only if the clip genuinely hinges on that word, and never in "
            "more than one option.",
            "- BANNED: short observational meme hooks ('bro thought he had it', 'wait for it'). "
            "BANNED: 'me when', 'POV:', 'that friend who'. "
            "BANNED: news-headline form ('Director X Does Y'). "
            "BANNED: titles under 6 words. BANNED: emoji. BANNED: hashtags.",
            "- PLAIN LANGUAGE (the audience is casual viewers, not film students): "
            "do NOT use film-craft jargon a normal viewer wouldn't say out loud. "
            "BANNED words include 'score', 'cinematography', 'mise-en-scene', "
            "'diegetic', 'blocking', 'third act', 'framing device', 'auteur'. "
            "Use the everyday word instead: 'music' (not 'score'), 'the way it's "
            "shot' or 'the shots' (not 'cinematography'), 'the ending' (not 'the "
            "third act'). Atmospheric and poetic is good; insider vocabulary is not.",
            "- The title is on-screen overlay text on a film still. It should feel like "
            "a film critic wrote it — poetic, specific, and atmospherically grounded — "
            "but in words any casual viewer instantly understands.",
        ]
    if style == "contextual_info":
        # Theanomalists titles: descriptive but conversational, not as long
        # as full Narrative news-headlines, but still naming the specific
        # moment with proper nouns where vision identified them.
        return [
            "- HARD RULE: each title is a clip-specific descriptive hook, "
            "5-12 words, that names the moment or sets up the curiosity. "
            "Use proper nouns when vision identified them (people, shows, "
            "games, events). Examples that fit: "
            "'Kevin Hart did NOT see this Druski joke coming', "
            "'The moment Ross finally yelled Pivot', "
            "'This Friends scene was actually improvised'.",
            "- BANNED: 'me ...', 'when YOU ...', 'pov:' — those are Meme "
            "Relatable framing. BANNED: encyclopedia openers ('X is a "
            "popular Y'). BANNED: pure noun-phrase labels with no verb "
            "('The Best Roast Moment', 'Funny Friends Clip').",
            "- The title is on-screen text: no hashtags and no emojis.",
        ]
    return [
        "- The title is on-screen text: no hashtags and no emojis.",
        "- Make it specific to the exact visible moment, never a vague description or a "
        "documentary-style label.",
    ]


def _meme_campaign_title_rules(style: str) -> list[str]:
    """Campaign-specific meme title hooks for Memeists Daily."""
    if style == "meme_friend_group":
        return [
            "- HARD RULE: each title is a friend/group-chat hook built for DM shares.",
            "- Use formats like 'that one friend who ...', 'the group chat when ...', "
            "'send this to the friend who ...', or 'my friends after ...'.",
            "- The viewer should instantly know which friend to send it to. "
            "No hashtags, no emojis, no factual headline voice.",
        ]
    if style == "meme_bro_main_character":
        return [
            "- HARD RULE: each title is a 'bro/main character' observation.",
            "- Use formats like 'bro really thought ...', 'bro had one chance', "
            "'bro is not built for this', or 'main character moment gone wrong'.",
            "- Keep it short, roast-y, and specific to the visible clip. "
            "No hashtags, no emojis, no news headline voice.",
        ]
    if style == "meme_chronically_online":
        return [
            "- HARD RULE: each title is about scrolling, algorithms, DMs, reels, "
            "notifications, or online habits.",
            "- Use formats like 'me opening reels for 5 minutes', "
            "'when the algorithm knows too much', or 'me sending 20 reels instead of replying'.",
            "- Make it feel painfully online and instantly relatable. "
            "No hashtags, no emojis, no factual headline voice.",
        ]
    if style == "meme_reaction_situation":
        return [
            "- HARD RULE: each title is a reaction/situation hook for the exact visible moment.",
            "- Use formats like 'when the joke is on you', 'when aging hits you', "
            "'when you realize it was your fault', or 'that moment you know it is over'.",
            "- The title should set up the situation, not explain the whole clip. "
            "No hashtags, no emojis, no factual headline voice.",
        ]
    if style == "meme_daily_cope":
        return [
            "- HARD RULE: each title fits the account's daily cope lane: tired, "
            "overconfident, pretending everything is fine, or barely holding it together.",
            "- Use formats like 'today's cope', 'me pretending everything is fine', "
            "'coping professionally', or 'daily dose of cope'.",
            "- Keep the tone funny and relatable, not motivational. "
            "No hashtags, no emojis, no factual headline voice.",
        ]
    return _caption_style_title_rules("meme_relatable")


def _name_the_thing_rules(vision_payload: dict[str, object] | None) -> list[str]:
    """Force the writer to use vision's extracted named entities verbatim
    instead of hedging with generic placeholders.

    Returns [] when vision found nothing nameable — the rule is a no-op
    in that case so it never asks the model to invent names it doesn't
    have. Applies across every caption_style because using real names is
    a universal quality lift (meme.ig, theanomalists, every observed good
    example named specific people/shows/companies).
    """
    if not vision_payload:
        return []
    named: list[str] = []
    seen: set[str] = set()
    for key in ("referenced_entity", "main_subject", "referenced_concept"):
        value = vision_payload.get(key)
        if isinstance(value, str):
            cleaned = _normalize_whitespace(value)
            # Skip empties and the vision schema's "not extracted" sentinels.
            if cleaned and cleaned.lower() not in {"(none)", "none", "n/a"} and cleaned not in seen:
                named.append(cleaned)
                seen.add(cleaned)
    if not named:
        return []
    names_list = ", ".join(f"'{name}'" for name in named[:4])
    return [
        "",
        "NAME THE THING (vision identified specific entities — use them):",
        f"- Vision extracted these named references: {names_list}.",
        "- You MUST use these names directly in the title and caption when "
        "they fit the moment. Do NOT hedge with generic placeholders like "
        "'this guy', 'a famous show', 'an actor', 'this comedian', "
        "'the streamer', or 'some game' when a real name is available "
        "above.",
        "- The one exception is the meme_relatable style, which deliberately "
        "uses 'this guy / her / they' in the caption body to keep the "
        "relatable framing — but even there, the on-screen title may use "
        "the real name if it fits.",
    ]


def _anti_explainer_avoid_lines(
    caption_style: str | None,
    account_voice: dict[str, str] | None,
) -> list[str]:
    """Extra AVOID bullets that block the encyclopedia-explainer drift
    observed on real meme.ig clips (Fix B).

    Skipped entirely for ``meme_factual`` because that style is deliberately
    Wikipedia-tone — applying these bans there would fight the style. For
    every other caption_style (default contextual_info, meme_relatable, hype,
    narrative) the model used to leak openers like "Minecraft is a popular
    sandbox game where..." and paste the account's ``target_audience`` string
    verbatim into captions. These bans plus the NEGATIVE/POSITIVE EXAMPLES
    block stop both drifts.
    """
    style = _normalize_caption_style(caption_style)
    # meme_factual is deliberately Wikipedia-tone. news_brief is entirely
    # fact-paragraphs (the whole style IS factual context). Skipping both
    # so the bans don't fight the styles' actual intent.
    # cinema_hook body is deliberately Wikipedia-style film synopsis — same skip.
    if style in {"meme_factual", "news_brief", "cinema_hook"}:
        return []
    lines = [
        "- Encyclopedia/explainer openers about the game, show, or format: "
        "'X is a popular Y game where...', 'Y is a sandbox game...', "
        "'In Z, players can...', 'A is a streaming platform where...'. "
        "Assume the viewer already knows the reference — do not define it.",
        # Widened ban (observed on real meme.ig clip output): the model used
        # to dodge the narrow 'X is a popular Y' pattern by switching to
        # academic 'X is a phenomenon' / 'The concept of X' framings. Catch
        # those explicitly so the dodge doesn't work either.
        "- Academic / sociological framing openers: 'X is a phenomenon...', "
        "'X is a concept...', 'X is a practice...', 'The concept of X...', "
        "'X has been observed...', 'X is a behavior that...', 'X is a "
        "trend that...'. These read like a Wikipedia stub. Lead with the "
        "feeling, situation, or specific moment instead.",
        "- Explain-the-joke openers: 'This clip is funny because...', "
        "'The humor comes from...', 'What makes this hilarious is...'. "
        "Show the moment instead of analysing it.",
    ]
    target_audience = _normalize_whitespace((account_voice or {}).get("target_audience", ""))
    if target_audience:
        # Inject the exact account-voice string into the ban so the model
        # cannot quietly paraphrase it back into the caption. This was the
        # 'Gen Z gamers and meme fans are always on the lookout...' leak
        # observed in PLAN §7.
        lines.append(
            "- Echoing the account audience description verbatim or as a close "
            f"paraphrase. Never write '{target_audience}' or anything like "
            "'<audience> are always on the lookout for...' in the caption — "
            "that string is metadata for you, not copy for the post."
        )
    else:
        lines.append(
            "- Echoing the account's target_audience setting verbatim or as a "
            "close paraphrase. Audience labels are metadata for you, not copy "
            "for the post."
        )
    return lines


def _negative_caption_examples_block(caption_style: str | None) -> list[str]:
    """Concrete bad/good caption opener examples to anchor the model.

    Skipped for ``meme_factual`` (intentionally encyclopedic) and
    ``narrative`` (long-form news article). For everything else, three
    bad/good pairs tend to be more effective than abstract bans alone —
    they give the model a calibration target.
    """
    style = _normalize_caption_style(caption_style)
    # news_brief is also intentionally fact-driven — its own examples live
    # in the news_brief caption_style_line and paragraph_rule.
    # cinema_hook body is intentionally encyclopedic film synopsis.
    if style in {
        "meme_factual",
        "narrative",
        "news_brief",
        "cinema_hook",
        "history_lost_archive",
    }:
        return []
    return [
        "",
        "NEGATIVE EXAMPLES (never write captions that open like these):",
        "- 'Minecraft is a popular sandbox game where players can build...' "
        "(encyclopedia opener)",
        "- 'The concept of sending unwanted content is a phenomenon that "
        "has been observed in various social media platforms...' "
        "(academic-framing opener — same Wikipedia drift, different wording)",
        "- 'Gen Z gamers and meme fans are always on the lookout for fresh "
        "content like this...' (audience-label leak)",
        "- 'This clip is funny because the trap finally worked...' " "(explain-the-joke)",
        "",
        "POSITIVE EXAMPLES (write captions that open like these):",
        "- 'That panic when the trap finally works and nobody knows who to blame.'",
        "- 'Bro really thought he had it figured out.'",
        "- 'POV: you spent two hours building this and it works first try.'",
    ]


def _caption_style_line(caption_style: str | None) -> str:
    style = _normalize_caption_style(caption_style)
    if style in _MEME_CAMPAIGN_STYLES:
        campaign_notes = {
            "meme_friend_group": (
                "Campaign lane: FRIEND GROUP MEMES. Aim every hook at a specific "
                "friend type or group-chat moment: 'that one friend who...', "
                "'the group chat when...', 'send this to the friend who...'."
            ),
            "meme_bro_main_character": (
                "Campaign lane: BRO / MAIN CHARACTER MEMES. Frame the clip as "
                "a short roast or main-character observation: 'bro really thought...', "
                "'bro had one chance', 'bro is not built for this'."
            ),
            "meme_chronically_online": (
                "Campaign lane: CHRONICALLY ONLINE MEMES. Connect the clip to "
                "scrolling, reels, algorithms, DMs, notifications, or sending too many reels."
            ),
            "meme_reaction_situation": (
                "Campaign lane: REACTION / SITUATION MEMES. Use broad situational "
                "hooks: 'when the joke is on you', 'when aging hits you', "
                "'when you realize it was your fault'."
            ),
            "meme_daily_cope": (
                "Campaign lane: DAILY COPE MEMES. Make the clip feel like funny "
                "overconfidence, pretending everything is fine, or barely holding it together. "
                "This lane should be longer than the other meme lanes: add 4-6 casual sentences "
                "of specific context, emotional cope, and why the moment is painfully relatable."
            ),
        }
        return (
            _caption_style_line("meme_relatable")
            + " "
            + campaign_notes.get(style, "")
            + " Every caption should pass this test: who would someone send this to?"
        )
    if style == "meme_relatable":
        return (
            "Caption emphasis: HYBRID — open with ONE relatable hook line "
            "(5-10 words, viewer is the subject: 'me when ...', 'when ...', "
            "'that friend who ...', 'pov: ...'). Then blank line. Then 2-3 "
            "short casual sentences of light context about what's HAPPENING in "
            "the clip (NOT encyclopedic — write like a friend texting you, "
            "not like Wikipedia). The hook drives shares, the context lets "
            "the Instagram algorithm categorize the post for Explore/FYP reach. "
            "Total: 40-80 words. End with a blank line then 3-5 hashtags. "
            "STRICT BANS for this style: do NOT mention celebrity names "
            "ANYWHERE in the caption (hook OR context body). Refer to people "
            "as 'this guy', 'her', 'this dude', 'they', 'this kid'. Do NOT "
            "use Wikipedia-style filler such as 'the act of', 'is a universal "
            "language', 'is a testament to', 'this clip showcases'. Do NOT "
            "explain WHY it's funny — just describe what's happening."
        )
    if style == "meme_factual":
        return (
            "Caption emphasis: follow the meme.ig template exactly. Line 1: a "
            "single emoji on its own (😂 default; 😭 for sad/cute moments; 🥹 "
            "for emotional; 🤣 for extra funny). Then a blank line. Then 2-3 "
            "short neutral, Wikipedia-style paragraphs that explain who/what is "
            "in the clip (50-120 words total). Tone is encyclopedic, not jokey. "
            "Do NOT comment on the joke; let the video carry it. Do NOT use "
            "'me when' framing. Hashtags optional (0-2)."
        )
    if style == "narrative":
        return (
            "Caption emphasis: follow the @theanomalists news-article template. "
            "Open with a direct narrative sentence (no emoji, no 'me when' hook) "
            "that names what happened. Then 2-4 short paragraphs separated by "
            "blank lines that tell the FULL STORY of this specific moment — who "
            "is involved, what occurred, when, and why it matters or what makes "
            "it shareable. Use real names, dates, and specific details. Length: "
            "100-200 words. Tone is journalistic, informative, like a short "
            "feature article. Do NOT use 'me when' framing. Do NOT use jokey "
            "encyclopedia openers. End with a blank line and 3-5 narrow hashtags."
        )
    if style == "news_brief":
        return (
            "Caption emphasis: follow the meme.ig news-brief template. Open "
            "with a SHORT engagement line (4-10 words, may end in 🤔 or 😅 or "
            "no emoji) on its own line. Then a blank line. Then 2-4 single-"
            "fact paragraphs about the subject — name the specific people, "
            "companies, shows, places, dates, and numbers; do not hedge. "
            "Each fact paragraph ENDS with 1-2 semantic topic emojis (💸 "
            "money, ⚡ tech, 🧠 AI, 📈 growth, 🎬 film, 📺 TV, 🔥 hype, 🏈 "
            "sports, 🎤 music) — chosen for meaning, never decoration. "
            "Length: 60-120 words. Hashtags: 0-2 (optional)."
        )
    if style == "cinema_hook":
        return (
            "Caption emphasis: follow the cinema.defined template exactly. "
            "Hook (line 1, 8-14 words): name ONE specific physical scene detail — a prop, "
            "a sound, a line of dialogue — then use '...' as a beat, then the emotional "
            "consequence, then ONE emoji (💀 twist, 🤯 reveal, 💔 tragedy). "
            "Example structure: '[Scene prop]... and [what it means]. [EMOJI]'. "
            "Synopsis body: open EVERY caption with "
            "'[Film Title] ([Year]), directed by [Director], is a [genre] film about [premise].' "
            "Then 2-3 encyclopedic paragraphs. Name ONLY the 2-3 cast members or "
            "characters that actually matter to THIS scene — never an exhaustive "
            "cast roster, which reads like padding. Land the FINAL paragraph on the "
            "specific moment in this clip (what it shows and why it stays with you), "
            "not a broad thesis about the whole film. "
            "Tone: IMDb/Letterboxd neutral — no opinion, no hype, no meme framing. "
            "Hashtags: 0-2 film-specific (e.g. #letterboxd #movietwt)."
        )
    if style == "history_lost_archive":
        return (
            "Caption emphasis: follow the Past Moments Daily template. Open "
            "like a specific moment from the past is being revisited, but the first "
            "sentence must name the exact subject, object, scene, or fact. "
            "Never use generic openers like 'Opening the archive', 'A "
            "forgotten fact', 'Here is the story', 'This is about', or 'In "
            "this clip'. Add clear context about the specific old clip, "
            "strange story, lost history, behind-the-scenes moment, or odd "
            "fact. Keep the voice curious but factual: lightly mysterious "
            "when the clip supports it, never dark for its own sake, never "
            "fake, never clickbait. Use 3 short paragraphs: specific history "
            "opener, context, then a payoff "
            "explaining why the detail matters, aged strangely, or became "
            "memorable. Length: 90-150 words. End with 3-5 narrow "
            "archive/history/topic hashtags."
        )
    if style == "hype":
        return (
            "Caption emphasis: emphasize the surprise, reveal, timing, or replay "
            "value, without exaggerating facts."
        )
    return (
        "Caption emphasis: lead with the relatable moment or feeling first, then add one "
        "concrete detail from THIS clip (a reaction, what happened, what someone said) "
        "before the payoff. Do NOT define the game/show/format and do NOT describe the "
        "audience — assume the viewer already knows the reference."
    )


def _profile_style_block(prompt_profile: str | None) -> dict[str, str]:
    profile = _normalize_whitespace(prompt_profile or "").casefold()
    if profile in {"gaming_meme", "reaction_clip"}:
        return {
            "style": (
                "Write like a meme and clip account such as meme.ig: casual, specific, and "
                "built around the exact visible joke, fail, or reaction. Sound like a person "
                "who watched the clip, not a brand."
            ),
            "title": (
                "Write the on-screen title as a relatable POV or situation hook that makes "
                "the viewer the subject. Use shapes like 'When ...', 'That one friend who "
                "...', 'Bro really thought ...', or 'POV: ...'. It can be a full specific "
                "sentence, but every word must point at the real visible situation. Never "
                "write a noun-phrase label such as 'X's Y Challenge' or 'The Best X Moment'."
            ),
            "caption": (
                "Write the caption as 3 short paragraphs that feel like a person reacting, "
                "not a teacher explaining. "
                "Paragraph 1 opens with the relatable feeling, situation, or 'we've all been "
                "there' moment — make the viewer the subject before naming the game or source. "
                "Paragraph 2 adds one specific concrete detail visible in THIS clip (a "
                "reaction, what went wrong, what someone said) — NOT a definition of the "
                "game/show/format, NOT an explanation of the joke, and NOT a description of "
                "the audience. Assume the viewer already knows what the game or show is. "
                "Paragraph 3 is the payoff or why the exact moment in this clip is shareable. "
                "No emojis."
            ),
        }
    if profile in {"lost_archive", "past_moments", "past_moments_daily"}:
        return {
            "style": (
                "Write like the editor of Past Moments Daily: curious, clear, factual, "
                "and easy to read. Make every post feel like a small story from the "
                "past about history moments, strange facts, old footage, behind-the-scenes "
                "details, music trivia, film trivia, or odd moments that aged interestingly."
            ),
            "title": (
                "Write the on-screen title as a short past-moment hook in 1-2 lines. "
                "Use curiosity, not clickbait: 'The lost story behind this scene', "
                "'This clip has a stranger backstory', or 'This old footage aged strangely'."
            ),
            "caption": (
                "Write the caption as 3 short paragraphs. Paragraph 1 opens with the "
                "strange, interesting, or forgotten moment, and must name the specific subject "
                "or fact instead of generic openers like 'Opening the archive' or 'A "
                "forgotten fact'. Paragraph 2 gives the clearest factual context supported "
                "by the clip and metadata. Paragraph 3 zooms in on why the detail matters, "
                "aged strangely, or became memorable. No fake facts, no 'you won't believe', "
                "no school-essay tone, and no meme framing."
            ),
        }
    if profile in {"cinema_study", "cinematic_study"}:
        return {
            "style": (
                "Write like a quiet cinema recommendation account: calm, specific, "
                "atmospheric, and grounded in the exact visible scene detail. The post "
                "should feel curated, not like a meme repost or entertainment headline."
            ),
            "title": (
                "Write the on-screen title as an atmospheric sentence about the feeling "
                "of watching this movie moment. Prefer silence, props, rewatch details, "
                "reveals, framing, and emotional consequence. No meme framing, no emoji, "
                "no hashtags."
            ),
            "caption": (
                "Write the caption as a scene-led movie recommendation: start from one "
                "specific visual or dialogue detail, then give only the supported film "
                "context, and end on why the moment makes the film worth remembering. "
                "Do not over-explain common references or invent unsupported movie facts."
            ),
        }
    if profile == "story_reel":
        return {
            "style": (
                "Write like an emotionally clear human-interest storyteller. Use less slang "
                "and more setup and payoff."
            ),
            "title": (
                "Write the on-screen title as an emotionally clear sentence hook that names "
                "the human moment. It may be a full sentence if it reads in 1-2 lines."
            ),
            "caption": (
                "Write the caption as a compact human-interest story in 2-3 short "
                "paragraphs. Paragraph 1 is the setup — open with the human moment, not a "
                "description of the video. Paragraph 2 is the transformation or payoff. "
                "An optional paragraph 3 is a short emotional close. No emojis."
            ),
        }
    return {
        "style": (
            "Write clean, widely understandable short-form copy with a strong hook and a "
            "clear payoff. Avoid generic filler."
        ),
        "title": (
            "Write the on-screen title as the cleanest direct hook for the visible moment. "
            "Keep it specific and readable in 1-2 lines."
        ),
        "caption": (
            "Write the caption in 2-3 short paragraphs. Paragraph 1 opens with the hook — "
            "the feeling, situation, or moment — before any context. Paragraph 2 is the "
            "context or payoff (keep any definition to one sentence). An optional paragraph 3 "
            "is why it is relatable or worth sharing. No emojis."
        ),
    }


def _tone_guidance(account_voice: dict[str, str] | None) -> str:
    base = (
        "Choose the tone that fits this exact clip: funny for fails, jokes, and meme "
        "reactions; question for relatable or debatable moments where a viewer would want "
        "to reply; emotional for transformations, nostalgia, or human-interest. Never force "
        "a tone the clip does not support."
    )
    lean = _normalize_whitespace((account_voice or {}).get("tone", ""))
    if lean:
        return (
            base
            + f" This account leans toward a {lean} tone, so prefer that when the clip allows it."
        )
    return base


def _groq_limit_profile() -> dict[str, object]:
    monthly_budget_usd = _float_env_value(
        "GROQ_MONTHLY_BUDGET_USD",
        default=DEFAULT_GROQ_MONTHLY_BUDGET_USD,
        minimum=0.01,
    )
    daily_video_cap = _int_env_value(
        "GROQ_DAILY_VIDEO_CAP",
        default=DEFAULT_GROQ_DAILY_VIDEO_CAP,
        minimum=1,
        maximum=1_000,
    )
    monthly_video_cap = _int_env_value(
        "GROQ_MONTHLY_VIDEO_CAP",
        default=DEFAULT_GROQ_MONTHLY_VIDEO_CAP,
        minimum=1,
        maximum=20_000,
    )
    budget_warn_ratio = _float_env_value(
        "GROQ_BUDGET_WARN_RATIO",
        default=DEFAULT_GROQ_BUDGET_WARN_RATIO,
        minimum=0.1,
        maximum=1.0,
    )
    return {
        "monthly_budget_usd": monthly_budget_usd,
        "monthly_video_cap": monthly_video_cap,
        "daily_video_cap": daily_video_cap,
        "budget_warn_at_usd": round(monthly_budget_usd * budget_warn_ratio, 4),
        "requests_per_full_video": GROQ_REQUESTS_PER_FULL_VIDEO,
        "max_frames_per_video": _groq_max_frames(),
        "profile": "free-basic-safe",
    }


def _float_env_value(
    key: str,
    *,
    default: float,
    minimum: float,
    maximum: float | None = None,
) -> float:
    raw_value = _normalize_whitespace(os.environ.get(key) or "")
    try:
        value = float(raw_value) if raw_value else default
    except ValueError:
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _int_env_value(
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int | None = None,
) -> int:
    raw_value = _normalize_whitespace(os.environ.get(key) or "")
    try:
        value = int(raw_value) if raw_value else default
    except ValueError:
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _grounding_guidance(*, transcript_text: str, vision_payload: dict[str, object] | None) -> str:
    has_visual_evidence = _has_visual_evidence(vision_payload)
    if transcript_text and has_visual_evidence:
        return (
            "Grounding mode: transcript + visual evidence. Cross-check both signals and make the draft match "
            "the visible moment, not just the spoken words."
        )
    if has_visual_evidence:
        return (
            "Grounding mode: visual-first silent/meme clip. Base the hook on the main subject, action, on-screen text, "
            "reaction, reveal, or payoff visible in the sampled frames."
        )
    if transcript_text:
        return "Grounding mode: transcript-first. Use the source title only to resolve ambiguity."
    return (
        "Grounding mode: metadata-only. Generate conservative working drafts and avoid pretending to know "
        "specific visual details."
    )


def _has_visual_evidence(vision_payload: dict[str, object] | None) -> bool:
    if not vision_payload:
        return False

    for key in (
        "scene_summary",
        "layout",
        "panel_relationship",
        "on_screen_hook",
        "implied_premise",
        "main_subject",
        "main_action",
        "tone",
        "uncertainty_notes",
    ):
        value = vision_payload.get(key)
        if isinstance(value, str) and value.strip() and value.strip() != "(none)":
            return True

    for key in ("visible_roles", "ocr_text", "hook_moments"):
        value = vision_payload.get(key)
        if isinstance(value, list) and any(str(item).strip() for item in value):
            return True

    return False


def _account_voice_prompt(account_voice: dict[str, str]) -> str:
    if not account_voice:
        return "Account voice settings: (none)"

    voice_lines = []
    ordered_keys = (
        ("clip_context", "Creator-provided clip premise"),
        ("tone", "Tone"),
        ("target_audience", "Target audience"),
        ("hook_style", "Hook style"),
        ("banned_phrases", "Avoid these phrases"),
        ("title_style", "Title style"),
        ("caption_style", "Caption style"),
    )
    for key, label in ordered_keys:
        value = account_voice.get(key)
        if value:
            voice_lines.append(f"{label}: {value}")
    if not voice_lines:
        return "Account voice settings: (none)"
    return "Account voice settings:\n- " + "\n- ".join(voice_lines)


def _build_groq_payload(
    *,
    model: str,
    transcript_text: str,
    source_title: str | None,
    source_description: str | None,
    niche_label: str | None,
    vision_payload: dict[str, object] | None,
    account_voice: dict[str, str],
    prompt_profile: str | None,
    caption_style: str | None,
    title_style: str | None,
    recent_titles: list[str] | None,
    recent_captions: list[str] | None,
    few_shot_winners: list[str] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "model": model,
        # Temperature lowered from 0.8 → 0.6: the writer still produces varied
        # hooks but is less likely to ramble past max_completion_tokens and
        # truncate the JSON mid-string. Combined with response_format below,
        # this should virtually eliminate the malformed-JSON fallback path.
        "temperature": 0.6,
        "max_completion_tokens": 1400,
        "top_p": 1,
        "stream": False,
        # Force the Groq API to return strict, parseable JSON. Without this,
        # Llama occasionally emits trailing commas, missing closing braces, or
        # markdown-fenced output — all of which tripped json.loads() and forced
        # the user into the lower-quality local rule-based fallback.
        # Requires the prompt to mention "json" somewhere (the system message
        # below already does — "Return only valid JSON...").
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": _smart_draft_system_prompt(caption_style),
            },
            {
                "role": "user",
                "content": _smart_draft_prompt(
                    transcript_text=transcript_text,
                    source_title=source_title,
                    source_description=source_description,
                    niche_label=niche_label,
                    vision_payload=vision_payload,
                    account_voice=account_voice,
                    prompt_profile=prompt_profile,
                    caption_style=caption_style,
                    title_style=title_style,
                    recent_titles=recent_titles,
                    recent_captions=recent_captions,
                    few_shot_winners=few_shot_winners,
                ),
            },
        ],
    }
    if model.startswith("openai/gpt-oss-"):
        payload["reasoning_effort"] = "medium"
    return payload


def _build_ollama_payload(
    *,
    model: str,
    transcript_text: str,
    source_title: str | None,
    source_description: str | None,
    niche_label: str | None,
    vision_payload: dict[str, object] | None,
    account_voice: dict[str, str],
    prompt_profile: str | None,
    caption_style: str | None,
    title_style: str | None,
    recent_titles: list[str] | None,
    recent_captions: list[str] | None,
    few_shot_winners: list[str] | None = None,
) -> dict[str, object]:
    return {
        "model": model,
        "stream": False,
        "format": "json",
        "messages": [
            {
                "role": "system",
                "content": _smart_draft_system_prompt(caption_style),
            },
            {
                "role": "user",
                "content": _smart_draft_prompt(
                    transcript_text=transcript_text,
                    source_title=source_title,
                    source_description=source_description,
                    niche_label=niche_label,
                    vision_payload=vision_payload,
                    account_voice=account_voice,
                    prompt_profile=prompt_profile,
                    caption_style=caption_style,
                    title_style=title_style,
                    recent_titles=recent_titles,
                    recent_captions=recent_captions,
                    few_shot_winners=few_shot_winners,
                ),
            },
        ],
    }


def _build_visual_summary_payload(
    *,
    model: str,
    transcript_text: str,
    source_title: str | None,
    niche_label: str | None,
    visual_frame_urls: list[str],
    source_description: str | None = None,
) -> dict[str, object]:
    user_content: list[dict[str, object]] = [
        {
            "type": "text",
            "text": (
                "Study these sampled video frames as a sequence from the clip and return only valid JSON. "
                "This is especially important when the clip has no useful dialogue, is a meme, or relies on a visual reaction. "
                "Identify the visible setup, subject, action, reaction, reveal, payoff, and any readable on-screen text. "
                "If the video is split-screen, duet, stitch, picture-in-picture, or reaction content, describe each panel separately and explain how they relate. "
                "Separate the original video premise from reaction/audio layers such as a guitarist, streamer, facecam, or commentator. "
                "Read the largest top caption or hook separately as on_screen_hook, even if other OCR text is also present. "
                "If the hook implies a joke premise, write that premise in implied_premise. For 'how can I get this job?', identify the visible role the viewer wants, such as driver, attendant, worker, or camera-side person, instead of saying the subject is applying. "
                "For meme/reaction clips, identify the referenced_entity when visible or inferable from reliable evidence, such as a movie, show, celebrity, game, character, trend, or meme format. "
                "Also identify referenced_concept and concept_definition when the joke depends on a phrase or idea such as changing skins, caught in 4K, rage quitting, or main character energy. "
                "Write meme_caption_premise as the exact relatable setup the top title should use, and context_explainer_seed as one conservative factual seed for the upload caption. "
                "If uncertain, leave these fields empty and explain uncertainty_notes instead of guessing. "
                "Use visible_roles to list who appears in the clip, including foreground/camera-side people and passengers. "
                "Also classify source text for preprocessing decisions. Dialogue subtitles and meme-joke text are content and should usually be kept. "
                "Locate the embedded video footage rectangle - the actual filmed or gameplay clip - and separate it from the surrounding black canvas, the original title text, and any caption or sub-line text. "
                "Report content_box as that footage rectangle in frame fractions: top and left are where the footage begins, bottom and right are where it ends (1.0 = the frame edge). "
                "If the footage already fills the whole frame, use top 0.0, left 0.0, bottom 1.0, right 1.0. "
                "Use this schema exactly: "
                '{"scene_summary":"","layout":"","panel_relationship":"","on_screen_hook":"","implied_premise":"","referenced_entity":"","referenced_concept":"","concept_definition":"","meme_caption_premise":"","context_explainer_seed":"","visible_roles":[],"ocr_text":[],"top_text_type":"meme_joke|source_title|watermark|channel_name|none","bottom_text_type":"subtitle|meme_joke|watermark|channel_name|none","keep_top_text":true,"keep_bottom_text":true,"suggested_title_layout":"no_title|top_band|overlay","content_box":{"top":0.0,"bottom":1.0,"left":0.0,"right":1.0},"crop_reason":"","main_subject":"","main_action":"","tone":"","confidence":"","hook_moments":[],"uncertainty_notes":""}. '
                "Keep values short and conservative.\n"
                f"Source title: {source_title or '(none)'}\n"
                f"Original source caption: {source_description or '(none)'}\n"
                f"Account niche: {niche_label or '(none)'}\n"
                f"Transcript context: {transcript_text or '(no transcript available)'}"
            ),
        }
    ]
    for frame_url in visual_frame_urls:
        user_content.append({"type": "image_url", "image_url": {"url": frame_url}})
    return {
        "model": model,
        "temperature": 0.2,
        "max_completion_tokens": 400,
        "top_p": 1,
        "stream": False,
        "messages": [
            {
                "role": "system",
                "content": "You summarize visible video content accurately and conservatively. Return JSON only.",
            },
            {
                "role": "user",
                "content": user_content,
            },
        ],
    }


def _all_groq_keys(*, preferred_first: str | None = None) -> list[str]:
    """Return all configured Groq API keys in priority order, de-duplicated.

    ``preferred_first`` (when provided) is placed at the head of the list so
    callers can rotate starting from the key they're currently using and only
    fall over to GROQ2 / GROQ3 on actual failure.
    """
    keys: list[str] = []
    seen: set[str] = set()
    candidates = [
        preferred_first,
        os.environ.get("GROQ_API_KEY"),
        os.environ.get("GROQ2_API_KEY"),
        os.environ.get("GROQ3_API_KEY"),
        os.environ.get("GROQ4_API_KEY"),
    ]
    for raw in candidates:
        cleaned = (raw or "").strip()
        if cleaned and cleaned not in seen:
            keys.append(cleaned)
            seen.add(cleaned)
    return keys


def _resolve_provider_order(
    model: str | None, api_key: str | None
) -> list[tuple[str, str, str | None]]:
    """Return ordered Groq attempts: primary key first, then GROQ2 and GROQ3 fallbacks.

    Ollama is no longer in the chain — the user wants Groq-only with rotating
    keys for rate-limit/quota failover. Multiple keys let us recover when one
    hits Groq's free-tier daily limit by silently switching to the next key.
    Same model and JSON mode are used for every attempt so quality is uniform.
    """
    providers: list[tuple[str, str, str | None]] = []
    resolved_model = model or os.environ.get("GROQ_MODEL") or DEFAULT_GROQ_MODEL

    # Try keys in order: explicit `api_key` parameter > GROQ_API_KEY >
    # GROQ2 > GROQ3 > GROQ4. De-duplicate so the same key isn't tried twice
    # if two env vars hold it. Add GROQ5/6/... here as you add more keys.
    candidate_keys: list[str] = []
    seen_keys: set[str] = set()
    for raw_key in (
        api_key,
        os.environ.get("GROQ_API_KEY"),
        os.environ.get("GROQ2_API_KEY"),
        os.environ.get("GROQ3_API_KEY"),
        os.environ.get("GROQ4_API_KEY"),
    ):
        cleaned = (raw_key or "").strip()
        if cleaned and cleaned not in seen_keys:
            candidate_keys.append(cleaned)
            seen_keys.add(cleaned)

    for key in candidate_keys:
        providers.append(("groq", resolved_model, key))
    return providers


def _ollama_enabled() -> bool:
    disabled = _normalize_whitespace(os.environ.get("OLLAMA_DISABLED") or "").lower()
    return disabled not in {"1", "true", "yes"}


def _groq_vision_enabled() -> bool:
    enabled = _normalize_whitespace(os.environ.get("GROQ_ENABLE_VISION") or "1").lower()
    return enabled not in {"0", "false", "no"}


def _groq_max_frames() -> int:
    raw_value = _normalize_whitespace(
        os.environ.get("GROQ_MAX_FRAMES") or str(DEFAULT_GROQ_MAX_FRAMES)
    )
    try:
        return max(1, min(MAX_GROQ_FRAMES_CAP, int(raw_value)))
    except ValueError:
        return DEFAULT_GROQ_MAX_FRAMES


def _request_timeout_seconds() -> int:
    raw_value = _normalize_whitespace(os.environ.get("GROQ_REQUEST_TIMEOUT_SECONDS") or "")
    try:
        return max(10, int(raw_value))
    except ValueError:
        return DEFAULT_REQUEST_TIMEOUT_SECONDS


def _retry_count() -> int:
    raw_value = _normalize_whitespace(os.environ.get("GROQ_RETRY_COUNT") or "")
    try:
        return max(0, min(3, int(raw_value)))
    except ValueError:
        return DEFAULT_RETRY_COUNT


def _ollama_chat_endpoint() -> str:
    base_url = _normalize_whitespace(os.environ.get("OLLAMA_BASE_URL") or DEFAULT_OLLAMA_BASE_URL)
    return f"{base_url.rstrip('/')}/api/chat"


def _perform_chat_completion_request(
    *,
    endpoint: str,
    headers: dict[str, str],
    payload: dict[str, object],
    provider_name: str,
) -> dict[str, object]:
    last_error: Exception | None = None
    for attempt in range(_retry_count() + 1):
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=_request_timeout_seconds()) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            last_error = RuntimeError(f"{provider_name} request failed: {exc.code} {body}".strip())
            if exc.code not in {408, 429, 500, 502, 503, 504} or attempt >= _retry_count():
                raise last_error from exc
            # Honor Groq's "try again in 234ms" / "try again in 2.344s" hint
            # embedded in the 429 body, plus the standard Retry-After header.
            # Without this, the retry fires instantly and hits the same TPM
            # limit window, defeating the purpose of retrying.
            wait_seconds = _retry_wait_seconds(exc, body, attempt)
            if wait_seconds > 0:
                time.sleep(wait_seconds)
        except urllib.error.URLError as exc:
            last_error = RuntimeError(f"{provider_name} request failed: {exc.reason}")
            if attempt >= _retry_count():
                raise last_error from exc
            time.sleep(_DEFAULT_BACKOFF_SECONDS * (attempt + 1))
    assert last_error is not None
    raise last_error


_DEFAULT_BACKOFF_SECONDS = 1.0
_RETRY_AFTER_HINT_RE = re.compile(r"try again in\s*([0-9]+(?:\.[0-9]+)?)\s*(ms|s)", re.IGNORECASE)


def _retry_wait_seconds(exc: urllib.error.HTTPError, body: str, attempt: int) -> float:
    """Compute how long to wait before the next retry.

    Priority: explicit hint in 429 body ("try again in 234ms") > Retry-After
    header > exponential backoff. Capped at 5s — anything longer means the
    user's free-tier window is genuinely exhausted and retrying won't help.
    """
    match = _RETRY_AFTER_HINT_RE.search(body or "")
    if match:
        value = float(match.group(1))
        unit = match.group(2).lower()
        seconds = value / 1000.0 if unit == "ms" else value
        return min(seconds + 0.05, 5.0)  # +50ms safety margin

    header_value = exc.headers.get("Retry-After") if exc.headers else None
    if header_value:
        try:
            return min(float(header_value), 5.0)
        except ValueError:
            pass

    return min(_DEFAULT_BACKOFF_SECONDS * (attempt + 1), 5.0)


def _extract_message_content(response_payload: dict[str, object]) -> str:
    if "message" in response_payload:
        message = response_payload["message"]
    else:
        choices = response_payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            return ""
        message = first_choice.get("message")

    if isinstance(message, dict):
        return _message_content_to_text(message.get("content"))
    return ""


def _message_content_to_text(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [_message_content_to_text(item) for item in content]
        return "\n".join(part for part in parts if part).strip()
    if isinstance(content, dict):
        text_value = content.get("text")
        if text_value:
            return _message_content_to_text(text_value)
        nested_content = content.get("content")
        if nested_content:
            return _message_content_to_text(nested_content)
    return ""


@dataclass(frozen=True)
class _ParsedDraftResponse:
    summary: str
    title_options: list[str]
    caption_options: list[str]
    recommended_title_index: int | None = None
    recommended_caption_index: int | None = None
    recommendation_reason: str | None = None
    option_notes: list[str] | None = None
    option_tiers: list[str] | None = None


# The model occasionally decorates a tier ("green ✅", "Tier: yellow") — match
# the colour word anywhere in the value rather than demanding an exact string,
# so a sloppy label still classifies instead of being dropped.
_OPTION_TIER_VALUES = ("green", "yellow", "red")


def _clean_option_tiers(value: object) -> list[str] | None:
    """Normalize the model's option_tiers into a list of 'green'/'yellow'/'red'.

    Anything unrecognized becomes 'yellow' — the conservative middle tier — so
    a malformed label is never silently treated as auto-postable 'green'.
    Returns ``None`` when no list was provided so callers can tell "model said
    nothing" apart from "model said green".
    """
    if not isinstance(value, (list, tuple)):
        return None
    tiers: list[str] = []
    for item in value:
        lowered = _normalize_whitespace(str(item)).casefold()
        matched = next((tier for tier in _OPTION_TIER_VALUES if tier in lowered), "yellow")
        tiers.append(matched)
        if len(tiers) >= SMART_DRAFT_OPTION_COUNT:
            break
    return tiers or None


def _parse_final_drafts(
    response_payload: dict[str, object], *, provider_name: str
) -> _ParsedDraftResponse:
    content = _extract_message_content(response_payload)
    parsed = _parse_model_json(content)
    summary = _normalize_whitespace(str(parsed.get("final_summary") or parsed.get("summary") or ""))
    title_options = _clean_options(
        parsed.get("title_options"),
        preserve_paragraphs=True,
        strip_wrapping_bold=True,
    )[:SMART_DRAFT_OPTION_COUNT]
    caption_options = _clean_options(parsed.get("caption_options"), preserve_paragraphs=True)[
        :SMART_CAPTION_OPTION_COUNT
    ]
    if (
        not summary
        or len(title_options) != SMART_DRAFT_OPTION_COUNT
        or len(caption_options) != SMART_CAPTION_OPTION_COUNT
    ):
        raise RuntimeError(f"{provider_name} did not return usable smart drafts.")
    recommendation = _parse_recommendation_fields(parsed)
    return _ParsedDraftResponse(
        summary=summary,
        title_options=title_options,
        caption_options=caption_options,
        recommended_title_index=recommendation["recommended_title_index"],
        recommended_caption_index=recommendation["recommended_caption_index"],
        recommendation_reason=recommendation["recommendation_reason"],
        option_notes=recommendation["option_notes"],
        option_tiers=recommendation["option_tiers"],
    )


def _parse_recommendation_fields(parsed: dict[str, object]) -> dict[str, object]:
    raw_pick = parsed.get("recommended_pick") or parsed.get("recommendation") or {}
    if not isinstance(raw_pick, dict):
        raw_pick = {}
    shared_index = _parse_recommendation_index(
        raw_pick.get("option_index")
        or raw_pick.get("option")
        or parsed.get("recommended_option_index")
        or parsed.get("recommended_option")
    )
    title_index = _parse_recommendation_index(
        raw_pick.get("title_option_index")
        or raw_pick.get("title_index")
        or parsed.get("recommended_title_option_index")
        or parsed.get("recommended_title_index")
    )
    caption_index = _parse_recommendation_index(
        raw_pick.get("caption_option_index")
        or raw_pick.get("caption_index")
        or parsed.get("recommended_caption_option_index")
        or parsed.get("recommended_caption_index")
    )
    reason = _normalize_whitespace(
        str(
            raw_pick.get("reason")
            or parsed.get("recommendation_reason")
            or parsed.get("recommended_reason")
            or ""
        )
    )
    option_notes = _clean_options(
        raw_pick.get("option_notes") or parsed.get("option_notes"),
        preserve_paragraphs=False,
    )[:SMART_DRAFT_OPTION_COUNT]
    option_tiers = _clean_option_tiers(raw_pick.get("option_tiers") or parsed.get("option_tiers"))
    return {
        "recommended_title_index": title_index if title_index is not None else shared_index,
        "recommended_caption_index": caption_index if caption_index is not None else shared_index,
        "recommendation_reason": reason[:320] or None,
        "option_notes": option_notes or None,
        "option_tiers": option_tiers,
    }


def _parse_recommendation_index(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        candidate = value
    else:
        text = _normalize_whitespace(str(value))
        if not text:
            return None
        match = re.search(r"\b([1-3])\b", text)
        if not match:
            return None
        candidate = int(match.group(1))
    if 1 <= candidate <= SMART_DRAFT_OPTION_COUNT:
        return candidate - 1
    if 0 <= candidate < SMART_DRAFT_OPTION_COUNT:
        return candidate
    return None


def _parse_vision_payload(
    response_payload: dict[str, object], *, provider_name: str
) -> dict[str, object]:
    content = _extract_message_content(response_payload)
    parsed = _parse_model_json(content)
    normalized = _empty_vision_payload()
    normalized["scene_summary"] = _normalize_whitespace(str(parsed.get("scene_summary") or ""))
    normalized["layout"] = _normalize_whitespace(str(parsed.get("layout") or ""))
    normalized["panel_relationship"] = _normalize_whitespace(
        str(parsed.get("panel_relationship") or "")
    )
    normalized["on_screen_hook"] = _normalize_whitespace(str(parsed.get("on_screen_hook") or ""))
    normalized["implied_premise"] = _normalize_whitespace(str(parsed.get("implied_premise") or ""))
    normalized["referenced_entity"] = _normalize_whitespace(
        str(parsed.get("referenced_entity") or "")
    )
    normalized["referenced_concept"] = _normalize_whitespace(
        str(parsed.get("referenced_concept") or "")
    )
    normalized["concept_definition"] = _normalize_whitespace(
        str(parsed.get("concept_definition") or "")
    )
    normalized["meme_caption_premise"] = _normalize_whitespace(
        str(parsed.get("meme_caption_premise") or "")
    )
    normalized["context_explainer_seed"] = _normalize_whitespace(
        str(parsed.get("context_explainer_seed") or "")
    )
    normalized["visible_roles"] = _clean_options(parsed.get("visible_roles"))
    normalized["ocr_text"] = _clean_options(parsed.get("ocr_text"))
    normalized["top_text_type"] = _vision_choice(
        parsed.get("top_text_type"),
        allowed={"meme_joke", "source_title", "watermark", "channel_name", "none"},
        default="none",
    )
    normalized["bottom_text_type"] = _vision_choice(
        parsed.get("bottom_text_type"),
        allowed={"subtitle", "meme_joke", "watermark", "channel_name", "none"},
        default="none",
    )
    normalized["keep_top_text"] = _bool_value(parsed.get("keep_top_text"), default=True)
    normalized["keep_bottom_text"] = _bool_value(parsed.get("keep_bottom_text"), default=True)
    normalized["suggested_title_layout"] = _vision_choice(
        parsed.get("suggested_title_layout"),
        allowed={"no_title", "top_band", "overlay"},
        default="top_band",
    )
    normalized["content_box"] = _normalize_content_box(parsed.get("content_box"))
    normalized["crop_reason"] = _normalize_whitespace(str(parsed.get("crop_reason") or ""))
    normalized["main_subject"] = _normalize_whitespace(str(parsed.get("main_subject") or ""))
    normalized["main_action"] = _normalize_whitespace(str(parsed.get("main_action") or ""))
    normalized["tone"] = _normalize_whitespace(str(parsed.get("tone") or ""))
    normalized["confidence"] = _normalize_whitespace(str(parsed.get("confidence") or ""))
    normalized["hook_moments"] = _clean_options(parsed.get("hook_moments"))
    normalized["uncertainty_notes"] = _normalize_whitespace(
        str(parsed.get("uncertainty_notes") or "")
    )
    if not any(
        [
            normalized["scene_summary"],
            normalized["layout"],
            normalized["panel_relationship"],
            normalized["on_screen_hook"],
            normalized["implied_premise"],
            normalized["referenced_entity"],
            normalized["referenced_concept"],
            normalized["concept_definition"],
            normalized["meme_caption_premise"],
            normalized["context_explainer_seed"],
            normalized["visible_roles"],
            normalized["ocr_text"],
            normalized["main_subject"],
            normalized["main_action"],
            normalized["hook_moments"],
        ]
    ):
        raise RuntimeError(f"{provider_name} did not return usable visual extraction.")
    return normalized


def _vision_choice(value: object, *, allowed: set[str], default: str) -> str:
    cleaned = _normalize_whitespace(str(value or "")).casefold()
    return cleaned if cleaned in allowed else default


def _bool_value(value: object, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    cleaned = _normalize_whitespace(str(value or "")).casefold()
    if cleaned in {"true", "1", "yes"}:
        return True
    if cleaned in {"false", "0", "no"}:
        return False
    return default


def _non_negative_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _bounded_float(value: object, *, max_value: float) -> float:
    try:
        return max(0.0, min(float(value), max_value))
    except (TypeError, ValueError):
        return 0.0


_FULL_FRAME_CONTENT_BOX: dict[str, float] = {
    "top": 0.0,
    "bottom": 1.0,
    "left": 0.0,
    "right": 1.0,
}


def _normalize_content_box(value: object) -> dict[str, float]:
    """Parse the vision footage rectangle. Falls back to the full frame when invalid."""
    if not isinstance(value, dict):
        return dict(_FULL_FRAME_CONTENT_BOX)
    top = _bounded_float(value.get("top"), max_value=1.0)
    left = _bounded_float(value.get("left"), max_value=1.0)
    bottom = _bounded_float(value.get("bottom"), max_value=1.0)
    right = _bounded_float(value.get("right"), max_value=1.0)
    # bottom/right default to the frame edge when the model omits them.
    if bottom <= top or right <= left:
        return dict(_FULL_FRAME_CONTENT_BOX)
    return {"top": top, "bottom": bottom, "left": left, "right": right}


def _parse_model_json(content: str) -> dict[str, object]:
    stripped = content.strip()
    if not stripped:
        raise RuntimeError("Model returned empty content.")
    stripped = re.sub(r"<think>.*?</think>", "", stripped, flags=re.DOTALL | re.IGNORECASE).strip()
    if not stripped:
        # Reasoning-style models occasionally return ONLY <think>...</think>
        # and no JSON body. Surface this as RuntimeError so the vision-key
        # rotation and low-context retry in _generate_groq_smart_drafts can
        # catch it — without this normalization the raw JSONDecodeError
        # escaped both retry paths and dumped straight to Local fallback.
        raise RuntimeError("Model returned only reasoning content, no JSON body.")
    fenced_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", stripped, re.DOTALL)
    if fenced_match:
        stripped = fenced_match.group(1)
    try:
        return _loads_model_json(stripped)
    except json.JSONDecodeError as exc:
        json_object = _extract_first_json_object(stripped)
        if json_object is None:
            # Normalize to RuntimeError so vision retry logic can catch it.
            raise RuntimeError(f"Model returned non-JSON content: {exc}") from exc
        try:
            return _loads_model_json(json_object)
        except json.JSONDecodeError as inner_exc:
            raise RuntimeError(f"Model returned malformed JSON: {inner_exc}") from inner_exc


def _loads_model_json(content: str) -> dict[str, object]:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # LLMs sometimes emit literal newlines inside JSON strings. Accept those
        # control characters rather than dropping to a lower-quality fallback.
        return json.loads(content, strict=False)


def _extract_first_json_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue
        if char == "{":
            depth += 1
            continue
        if char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _clean_options(
    value: object,
    *,
    preserve_paragraphs: bool = False,
    strip_wrapping_bold: bool = False,
) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    for item in value:
        # smaller models sometimes wrap each option in {"caption":"..."} or {"title":"..."}
        if isinstance(item, dict):
            item = next((v for v in item.values() if isinstance(v, str)), None) or str(item)
        text = (
            _normalize_caption_text(str(item))
            if preserve_paragraphs
            else _normalize_whitespace(str(item))
        )
        if (
            strip_wrapping_bold
            and text.startswith("**")
            and text.endswith("**")
            and text.count("**") == 2
        ):
            text = text[2:-2].strip()
        if text:
            cleaned.append(text)
    return cleaned


def _summarize_visual_frames_for_local_generation(
    *,
    visual_frame_urls: list[str],
    source_title: str | None,
    niche_label: str | None,
) -> str | None:
    if not visual_frame_urls:
        return None
    frame_count = min(len(visual_frame_urls), _groq_max_frames())
    summary_bits = [
        f"{frame_count} sampled frames are available from across the clip.",
        "Treat them as sequential evidence of the same short video.",
    ]
    if source_title:
        summary_bits.append(f"Source-title hint: {source_title}.")
    if niche_label:
        summary_bits.append(f"Niche hint: {niche_label}.")
    summary_bits.append(
        "Stay grounded in the visible moment and avoid inventing unsupported details."
    )
    return " ".join(summary_bits)


def _fallback_vision_payload(
    *,
    source_title: str | None,
    niche_label: str | None,
    visual_frame_urls: list[str],
) -> dict[str, object] | None:
    summary = _summarize_visual_frames_for_local_generation(
        visual_frame_urls=visual_frame_urls,
        source_title=source_title,
        niche_label=niche_label,
    )
    if not summary:
        return None
    payload = _empty_vision_payload()
    payload["scene_summary"] = summary
    payload["confidence"] = "low"
    payload[
        "uncertainty_notes"
    ] = "Derived from frame availability only; no structured vision model output."
    return payload


def _generate_local_fallback_drafts(
    *,
    transcript_text: str,
    source_title: str | None,
    source_description: str | None,
    niche_label: str | None,
    visual_summary: str | None,
    account_voice: dict[str, str],
    prompt_profile: str | None,
    caption_style: str | None,
    title_style: str | None = None,
    recent_titles: list[str] | None,
    recent_captions: list[str] | None,
    errors: list[str],
    low_context: bool = False,
    frame_count: int = 0,
) -> SmartDrafts:
    base_title = _normalize_whitespace(source_title or "") or "Video Clip"
    niche_text = _normalize_whitespace(niche_label or "") or "short-form content"
    source_description_text = _normalize_whitespace(source_description or "")
    summary_signal = (
        _normalize_whitespace(visual_summary or "")
        or _summarize_from_transcript(transcript_text)
        or source_description_text
    )
    summary = summary_signal or f"A {niche_text} clip built from the current source context."
    title_options = _fallback_title_options(
        base_title=base_title, niche_text=niche_text, summary=summary
    )
    caption_options = _fallback_caption_options(
        base_title=base_title,
        niche_text=niche_text,
        summary=summary,
        transcript_text=transcript_text,
        account_voice=account_voice,
        caption_style=caption_style,
    )
    fallback_option_notes = [
        "Safest fallback pick from available source context.",
        "Alternative angle if the first option feels too direct.",
        "Backup option for a broader hook.",
    ]
    fallback_reason = (
        f"Best fallback pick for {niche_text} because it uses the clearest available subject "
        "without inventing unsupported details."
    )
    # Fallback drafts are produced without a model grader, so we cannot vouch
    # for tier accuracy — tag every option 'yellow' (review before posting)
    # rather than 'green', so the auto-publish gate never ships a degraded
    # fallback draft unattended.
    fallback_option_tiers = ["yellow"] * SMART_DRAFT_OPTION_COUNT
    return SmartDrafts(
        summary=summary,
        title_options=title_options,
        caption_options=caption_options,
        provider_label="Local fallback",
        recommended_title_index=0,
        recommended_caption_index=0,
        recommendation_reason=fallback_reason,
        option_notes=fallback_option_notes,
        option_tiers=fallback_option_tiers,
        used_fallback=True,
        generation_meta={
            "writer_model": None,
            "vision_model": None,
            "frame_count": frame_count,
            "vision_attempted": frame_count > 0,
            "vision_used": False,
            "vision_retry_attempted": False,
            "vision_error": " | ".join(errors) if errors else None,
            "low_context": low_context,
            "caption_style": caption_style,
            "title_style": title_style,
            "recommended_title_option_index": 0,
            "recommended_caption_option_index": 0,
            "recommendation_reason": fallback_reason,
            "option_notes": fallback_option_notes,
            "option_tiers": fallback_option_tiers,
            "errors": errors,
        },
    )


def _summarize_from_transcript(transcript_text: str) -> str:
    cleaned = _normalize_whitespace(transcript_text)
    if not cleaned:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    return _normalize_whitespace(" ".join(sentences[:2]))[:220]


def _fallback_title_options(*, base_title: str, niche_text: str, summary: str) -> list[str]:
    title_root = _trim_title_phrase(base_title)
    summary_root = _trim_title_phrase(summary)
    options = [
        title_root,
        _trim_title_phrase(f"{niche_text.title()} Hook"),
        summary_root,
    ]
    deduped: list[str] = []
    for option in options:
        text = _normalize_whitespace(option)[:60]
        if text and text.lower() not in {item.lower() for item in deduped}:
            deduped.append(text)
    while len(deduped) < SMART_DRAFT_OPTION_COUNT:
        deduped.append(f"{title_root} Moment")
    return deduped[:SMART_DRAFT_OPTION_COUNT]


def _fallback_caption_options(
    *,
    base_title: str,
    niche_text: str,
    summary: str,
    transcript_text: str,
    account_voice: dict[str, str],
    caption_style: str | None = None,
) -> list[str]:
    # Build a one-sentence context line from transcript if available, else skip it.
    transcript_summary = _summarize_from_transcript(transcript_text)
    context_line = f"{transcript_summary}" if transcript_summary else ""
    hashtags = _fallback_hashtag_line(base_title=base_title, niche_text=niche_text)

    # Three caption angles: relatable hook, payoff focus, plain context.
    # All read as real Instagram copy — no internal meta-text.
    option_1_parts = [summary]
    if context_line:
        option_1_parts.append(context_line)
    option_1_parts.append(hashtags)

    option_2_parts = [f"This is the kind of clip you don't see coming. 👀"]
    option_2_parts.append(
        f"{base_title} — one of those {niche_text} moments that's hard to explain "
        "until you actually watch it."
    )
    option_2_parts.append(hashtags)

    option_3_parts = [f"Sometimes the simplest clips are the most rewatchable."]
    option_3_parts.append(
        f"If you're into {niche_text}, this one's worth a second look. "
        f"{base_title} delivers exactly what you'd hope for. 🎯"
    )
    option_3_parts.append(hashtags)

    options = [
        "\n\n".join(p for p in option_1_parts if p),
        "\n\n".join(p for p in option_2_parts if p),
        "\n\n".join(p for p in option_3_parts if p),
    ]
    return [_normalize_caption_text(option) for option in options][:SMART_CAPTION_OPTION_COUNT]


def _fallback_caption_style_sentence(caption_style: str | None) -> str:
    style = _normalize_caption_style(caption_style)
    if style == "contextual_info":
        return "Use a context-first caption that explains the reference before the punchline."
    if style == "meme_relatable":
        return "Use a short 'me/when/that friend' caption that makes the viewer the subject."
    if style == "meme_factual":
        return "Use the meme.ig template: one emoji on its own line, then 2-3 neutral Wikipedia-style paragraphs."
    if style == "narrative":
        return "Use the @theanomalists news-article template: long narrative headline title plus 2-4 paragraphs telling the full story of the moment."
    if style == "hype":
        return "Use a high-energy caption that highlights the reveal or replay value."
    return "Use a grounded caption that explains the visible moment clearly."


def _fallback_hashtag_line(*, base_title: str, niche_text: str) -> str:
    source = f"{base_title} {niche_text}".lower()
    tags: list[str] = []
    if any(
        keyword in source
        for keyword in (
            "family",
            "grandpa",
            "grandfather",
            "grandma",
            "memory",
            "childhood",
            "history",
            "photo",
            "restore",
            "ai",
        )
    ):
        tags.extend(["#family", "#history", "#childhood", "#memories", "#aitools"])
    elif any(
        keyword in source for keyword in ("minecraft", "game", "gaming", "roblox", "fortnite")
    ):
        tags.extend(["#gaming", "#minecraft", "#gameplay", "#reels", "#clips"])
    elif any(keyword in source for keyword in ("animal", "pet", "cat", "dog", "zoo")):
        tags.extend(["#animals", "#pets", "#reels", "#funny", "#clips"])
    else:
        tags.extend(["#reels", "#shorts", "#viralvideos", "#clips", "#fyp"])
    deduped: list[str] = []
    for tag in tags:
        if tag not in deduped:
            deduped.append(tag)
    return " ".join(deduped[:5])


def _trim_title_phrase(text: str) -> str:
    cleaned = _normalize_whitespace(text)
    if not cleaned:
        return "Video Clip"
    trimmed = re.sub(r"[^\w\s'-]", "", cleaned).strip()
    words = trimmed.split()
    if not words:
        return "Video Clip"
    return " ".join(words[:6])


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _normalize_caption_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in normalized.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _normalize_account_voice(account_voice: dict[str, str] | None) -> dict[str, str]:
    if not account_voice:
        return {}
    normalized: dict[str, str] = {}
    for key, value in account_voice.items():
        cleaned = _normalize_whitespace(value)
        if cleaned:
            normalized[key] = cleaned
    return normalized


def _empty_vision_payload() -> dict[str, object]:
    return {
        "scene_summary": "(none)",
        "layout": "",
        "panel_relationship": "",
        "on_screen_hook": "",
        "implied_premise": "",
        "referenced_entity": "",
        "referenced_concept": "",
        "concept_definition": "",
        "meme_caption_premise": "",
        "context_explainer_seed": "",
        "visible_roles": [],
        "ocr_text": [],
        "top_text_type": "none",
        "bottom_text_type": "none",
        "keep_top_text": True,
        "keep_bottom_text": True,
        "suggested_title_layout": "top_band",
        "content_box": {"top": 0.0, "bottom": 1.0, "left": 0.0, "right": 1.0},
        "crop_reason": "",
        "main_subject": "",
        "main_action": "",
        "tone": "",
        "confidence": "none",
        "hook_moments": [],
        "uncertainty_notes": "",
    }


def _niche_profile(niche_label: str | None) -> str:
    niche = _normalize_whitespace(niche_label or "").lower()
    if not niche:
        return (
            "Prefer clean, widely understandable short-form writing with a strong hook, "
            "clear payoff, and no generic filler."
        )

    # History must be detected before the podcast/story branch below: the
    # history niche_label often contains "stories" ("forgotten stories"), which
    # would otherwise misroute history footage into talking-head guidance.
    is_history = _is_history_niche(niche_label)

    profile_parts: list[str] = []
    if is_history:
        profile_parts.append(
            "Write like a history page that makes ordinary past life feel worth "
            "watching. NAME the visible subject (object, person, activity, "
            "vehicle, place, technology) and pair it with the one reason it is "
            "surprising, nostalgic, emotionally human, or different from today — "
            "do not merely label what is on screen. The on-screen hook explains "
            "WHY the footage is worth watching, in plain, instantly readable "
            "words. Ground every claim in what is visible or verified: never "
            "invent rarity, disappearance, first-ever status, popularity, or "
            "historical importance, and use an exact year or decade only when it "
            "is provided or verified. No meme framing, no clickbait, no emoji or "
            "hashtags in the on-screen title."
        )
    if any(keyword in niche for keyword in ("game", "gaming", "minecraft", "roblox", "fortnite")):
        profile_parts.append(
            "Use energetic gameplay language, highlight the moment, mechanic, fail, win, or payoff, "
            "and avoid sounding like a generic streamer title."
        )
    if any(keyword in niche for keyword in ("comedy", "funny", "meme")):
        profile_parts.append(
            "Write like a real meme page: short, casual, internet-native, and specific to "
            "the joke. Lean into timing, surprise, and the funniest observable detail "
            "instead of broad hype. Reference the visible game, show, person, format, or "
            "situation only when the evidence supports it. Do not explain the joke too "
            "much, do not sound like a brand, and avoid polished creator-marketing phrasing. "
            "One valid caption style is deliberate ironic over-explanation: pick the most "
            "obvious concept in the clip and describe it in 2-4 sentences of deadpan, "
            "Wikipedia-style formality. The humour comes from the contrast between the "
            "serious tone and the absurd or simple video. Use this when it fits the clip."
        )
    if any(keyword in niche for keyword in ("tutorial", "education", "how to", "guide")):
        profile_parts.append(
            "Make the value obvious quickly and favor clarity, outcome, and practical phrasing."
        )
    if any(keyword in niche for keyword in ("motivation", "mindset", "self improvement")):
        profile_parts.append(
            "Use direct, emotionally clear language with a strong takeaway and avoid empty inspiration cliches."
        )
    if not is_history and any(
        keyword in niche for keyword in ("podcast", "interview", "commentary", "story")
    ):
        profile_parts.append(
            "Emphasize the sharpest idea, reveal, or quote-worthy takeaway rather than generic recap language."
        )
    if any(keyword in niche for keyword in ("animal", "pet", "nature")):
        profile_parts.append(
            "Highlight the most visible behavior, reaction, or reveal and keep the tone warm and specific."
        )
    if any(keyword in niche for keyword in ("movie", "film", "cinema", "tv", "series", "show")):
        profile_parts.append(
            "Write like a cinephile who has seen the film multiple times and can name the exact scene, "
            "prop, or line that made it memorable. The on-screen title should evoke the FEELING of watching "
            "that moment — atmospheric, reflective, never a plot summary. The caption hook names one specific "
            "physical detail from the scene (a prop, a look, a line) that rewards people who've seen it. "
            "The synopsis body is encyclopedic and neutral — name the director, year, genre, and core themes. "
            "Do not use meme framing, gaming language, or generic hype phrases."
        )

    if not profile_parts:
        profile_parts.append(
            "Use language that feels natural for the niche, emphasize the most concrete payoff, and avoid generic hooks."
        )
    return " ".join(profile_parts)


def _angle_plan(niche_label: str | None) -> str:
    niche = _normalize_whitespace(niche_label or "").lower()
    if _is_history_niche(niche_label):
        return (
            "Option 1 = curiosity / surprising fact: name the visible subject and "
            "the one detail that makes a viewer think 'wait, that existed?'. "
            "Option 2 = nostalgia / everyday-life framing: present the footage as "
            "how people once lived, traveled, worked, shopped, or celebrated. "
            "Option 3 = modern comparison, emotional human moment, or a "
            "comment-prompt question — only when the footage or verified context "
            "supports it."
        )
    if any(keyword in niche for keyword in ("game", "gaming", "minecraft", "roblox", "fortnite")):
        return (
            "Option 1 = direct gameplay hook. "
            "Option 2 = curiosity around the mechanic, trick, or outcome. "
            "Option 3 = payoff/result framing that explains why the clip is worth watching."
        )
    if any(keyword in niche for keyword in ("comedy", "funny", "meme")):
        return (
            "Option 1 = punchline-first or reaction hook — lead with the funniest line, no setup needed. "
            "Option 2 = 'when...' or POV setup the audience instantly recognises from their own life. "
            "Option 3 = ironic over-explanation: pick the most obvious concept in the clip and write "
            "2-4 sentences describing it in deadpan Wikipedia-style formality. "
            "The contrast between the serious tone and the dumb/simple video is the joke."
        )
    if any(keyword in niche for keyword in ("tutorial", "education", "how to", "guide")):
        return (
            "Option 1 = clearest practical value hook. "
            "Option 2 = curiosity around the method or shortcut. "
            "Option 3 = explanation or result framing that highlights the outcome."
        )
    if any(keyword in niche for keyword in ("movie", "film", "cinema", "tv", "series", "show")):
        return (
            "Option 1 = the universal FEELING angle: a 'That kind of...' title about the emotional experience "
            "of watching the moment — what the viewer FEELS, not what happens on screen. "
            "Option 2 = the rewatch angle: title focuses on what you only notice on a second viewing — "
            "the detail that was there all along but invisible the first time. "
            "Option 3 = the silence/impact angle: title about the moment the film stops and makes you think — "
            "no dialogue needed, just the weight of what was shown."
        )
    return (
        "Option 1 = strongest direct hook. "
        "Option 2 = curiosity-driven angle. "
        "Option 3 = explanatory, observational, or payoff angle."
    )
