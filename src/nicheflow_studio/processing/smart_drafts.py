from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from nicheflow_studio.processing.video import sample_video_frame_data_urls


DEFAULT_OLLAMA_MODEL = "qwen2.5:7b"
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"
DEFAULT_GROQ_VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
SMART_DRAFT_OPTION_COUNT = 3
SMART_CAPTION_OPTION_COUNT = 2
SMART_CAPTION_SENTENCE_TARGET = "3-5"
DEFAULT_GROQ_MAX_FRAMES = 3
MAX_GROQ_FRAMES_CAP = 5
DEFAULT_REQUEST_TIMEOUT_SECONDS = 90
DEFAULT_RETRY_COUNT = 1
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


@dataclass(frozen=True)
class SmartDrafts:
    summary: str
    title_options: list[str]
    caption_options: list[str]
    provider_label: str
    used_fallback: bool = False
    vision_payload: dict[str, object] | None = None
    generation_meta: dict[str, object] | None = None


def generate_smart_drafts(
    *,
    transcript_text: str,
    source_title: str | None,
    niche_label: str | None,
    input_path: Path | None = None,
    model: str | None = None,
    api_key: str | None = None,
    account_voice: dict[str, str] | None = None,
) -> SmartDrafts:
    cleaned_transcript = _normalize_whitespace(transcript_text)
    cleaned_source_title = _normalize_whitespace(source_title or "")
    cleaned_niche = _normalize_whitespace(niche_label or "")
    normalized_voice = _normalize_account_voice(account_voice)
    if not any([cleaned_transcript, cleaned_source_title, cleaned_niche, normalized_voice]):
        raise RuntimeError("Not enough context to generate smart drafts.")

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
                return _generate_groq_smart_drafts(
                    api_key=resolved_api_key or "",
                    reasoning_model=resolved_model,
                    transcript_text=cleaned_transcript,
                    source_title=cleaned_source_title or None,
                    niche_label=cleaned_niche or None,
                    visual_frame_urls=visual_frame_urls,
                    account_voice=normalized_voice,
                )
            return _generate_ollama_smart_drafts(
                model=resolved_model,
                transcript_text=cleaned_transcript,
                source_title=cleaned_source_title or None,
                niche_label=cleaned_niche or None,
                visual_frame_urls=visual_frame_urls,
                account_voice=normalized_voice,
            )
        except Exception as exc:
            errors.append(str(exc))

    if errors:
        visual_summary = _summarize_visual_frames_for_local_generation(
            visual_frame_urls=visual_frame_urls,
            source_title=cleaned_source_title or None,
            niche_label=cleaned_niche or None,
        )
        return _generate_local_fallback_drafts(
            transcript_text=cleaned_transcript,
            source_title=cleaned_source_title or None,
            niche_label=cleaned_niche or None,
            visual_summary=visual_summary,
            account_voice=normalized_voice,
            errors=errors,
        )

    raise RuntimeError("Smart draft generation failed: No smart-draft provider is configured.")


def can_generate_smart_drafts() -> bool:
    return bool(os.environ.get("GROQ_API_KEY")) or _ollama_enabled()


def _generate_ollama_smart_drafts(
    *,
    model: str,
    transcript_text: str,
    source_title: str | None,
    niche_label: str | None,
    visual_frame_urls: list[str],
    account_voice: dict[str, str],
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
            niche_label=niche_label,
            vision_payload=visual_payload,
            account_voice=account_voice,
        ),
        provider_name=f"Ollama model {model}",
    )
    parsed = _parse_final_drafts(response_payload, provider_name="Ollama")
    return SmartDrafts(
        summary=parsed.summary,
        title_options=parsed.title_options,
        caption_options=parsed.caption_options,
        provider_label="Ollama Qwen 2.5 7B",
        vision_payload=visual_payload,
        generation_meta={
            "writer_model": model,
            "vision_model": None,
            "frame_count": len(visual_frame_urls),
            "vision_used": bool(visual_frame_urls),
        },
    )


def _generate_groq_smart_drafts(
    *,
    api_key: str,
    reasoning_model: str,
    transcript_text: str,
    source_title: str | None,
    niche_label: str | None,
    visual_frame_urls: list[str],
    account_voice: dict[str, str],
) -> SmartDrafts:
    vision_model = os.environ.get("GROQ_VISION_MODEL") or DEFAULT_GROQ_VISION_MODEL
    vision_payload: dict[str, object] | None = None
    vision_response: dict[str, object] | None = None
    vision_error: str | None = None
    if _groq_vision_enabled() and visual_frame_urls:
        try:
            vision_response = _perform_chat_completion_request(
                endpoint=GROQ_CHAT_COMPLETIONS_URL,
                headers=_groq_headers(api_key),
                payload=_build_visual_summary_payload(
                    model=vision_model,
                    transcript_text=transcript_text,
                    source_title=source_title,
                    niche_label=niche_label,
                    visual_frame_urls=visual_frame_urls[: _groq_max_frames()],
                ),
                provider_name=f"Groq vision model {vision_model}",
            )
            vision_payload = _parse_vision_payload(vision_response, provider_name="Groq vision")
        except RuntimeError as exc:
            vision_error = str(exc)
            vision_payload = None

    writer_response = _perform_chat_completion_request(
        endpoint=GROQ_CHAT_COMPLETIONS_URL,
        headers=_groq_headers(api_key),
        payload=_build_groq_payload(
            model=reasoning_model,
            transcript_text=transcript_text,
            source_title=source_title,
            niche_label=niche_label,
            vision_payload=vision_payload,
            account_voice=account_voice,
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
        vision_payload=vision_payload,
        generation_meta={
            "writer_model": reasoning_model,
            "vision_model": vision_model if vision_payload else None,
            "frame_count": len(visual_frame_urls[: _groq_max_frames()]),
            "vision_used": vision_payload is not None,
            "vision_error": vision_error,
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
    vision_payload: dict[str, object] | None = None,
    account_voice: dict[str, str] | None = None,
) -> str:
    source_title_text = source_title or "(none)"
    niche_text = niche_label or "(none)"
    transcript_block = transcript_text if transcript_text else "(no transcript available)"
    niche_profile = _niche_profile(niche_label)
    angle_plan = _angle_plan(niche_label)
    niche_guidance = (
        f"Write like someone who understands the {niche_text} niche. Use wording, framing, and hooks that feel native to that audience."
        if niche_label
        else "Write in a broadly engaging short-form style without sounding generic or spammy."
    )
    transcript_guidance = (
        "Use the transcript as the primary signal when it is present."
        if transcript_text
        else (
            "Visual-first mode: no transcript is available. Infer the clip context from structured visual evidence first, "
            "then use the source title and niche as supporting hints. Still stay conservative and grounded."
        )
    )
    grounding_guidance = _grounding_guidance(
        transcript_text=transcript_text,
        vision_payload=vision_payload,
    )
    voice_block = _account_voice_prompt(account_voice or {})
    vision_block = json.dumps(
        vision_payload or _empty_vision_payload(),
        ensure_ascii=False,
        sort_keys=True,
    )
    return (
        "Use every available signal to understand the clip and generate upload-friendly drafts.\n"
        f"Source title: {source_title_text}\n"
        f"Account niche: {niche_text}\n"
        f"Niche guidance: {niche_guidance}\n"
        f"Niche style profile: {niche_profile}\n"
        f"Option angle plan: {angle_plan}\n"
        f"Visual evidence JSON: {vision_block}\n"
        f"{voice_block}\n"
        "Treat the visual evidence JSON as the primary visual grounding. If it is empty, do not invent details.\n"
        f"{grounding_guidance}\n"
        "Requirements:\n"
        "- final_summary: 1 short sentence about what the clip is about\n"
        f"- title_options: exactly {SMART_DRAFT_OPTION_COUNT} short, punchy top-title options strong enough for on-screen text\n"
        f"- caption_options: exactly {SMART_CAPTION_OPTION_COUNT} distinct, social-ready caption options for upload description or pinned-comment style copy\n"
        "- Keep titles tight, usually 4-10 words, and front-load the strongest words\n"
        "- Keep captions self-contained, conversational, and specific to the visible moment\n"
        f"- Each caption should be {SMART_CAPTION_SENTENCE_TARGET} sentences when context supports it\n"
        "- Captions should include a clear setup, the visible payoff or reaction, and a natural final line that invites comments without generic CTA spam\n"
        "- Make caption option 1 more direct and punchy; make caption option 2 more story-like or curiosity-driven\n"
        "- Make the options meaningfully different from one another, not light rewrites\n"
        "- Favor concrete nouns and verbs over vague hype words\n"
        "- For silent or meme-style clips, describe the visible setup, reaction, reveal, or payoff instead of forcing dialogue-based framing\n"
        "- Do not write as if the clip has spoken narration when the transcript is missing or empty\n"
        "- Do not invent facts that are not supported by the transcript, title, niche context, or visual evidence JSON\n"
        "- Avoid generic CTA spam such as 'like and follow' unless clearly requested in the account voice settings\n\n"
        f"Guidance: {transcript_guidance}\n\n"
        f"Transcript:\n{transcript_block}"
    )


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

    for key in ("scene_summary", "main_subject", "main_action", "tone", "uncertainty_notes"):
        value = vision_payload.get(key)
        if isinstance(value, str) and value.strip() and value.strip() != "(none)":
            return True

    for key in ("ocr_text", "hook_moments"):
        value = vision_payload.get(key)
        if isinstance(value, list) and any(str(item).strip() for item in value):
            return True

    return False


def _account_voice_prompt(account_voice: dict[str, str]) -> str:
    if not account_voice:
        return "Account voice settings: (none)"

    voice_lines = []
    ordered_keys = (
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
    niche_label: str | None,
    vision_payload: dict[str, object] | None,
    account_voice: dict[str, str],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "model": model,
        "temperature": 0.8,
        "max_completion_tokens": 700,
        "top_p": 1,
        "stream": False,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You create short-form video hooks and captions. "
                    "Return only valid JSON with keys final_summary, title_options, caption_options. "
                    f"title_options must contain exactly {SMART_DRAFT_OPTION_COUNT} strings. "
                    f"caption_options must contain exactly {SMART_CAPTION_OPTION_COUNT} strings."
                ),
            },
            {
                "role": "user",
                "content": _smart_draft_prompt(
                    transcript_text=transcript_text,
                    source_title=source_title,
                    niche_label=niche_label,
                    vision_payload=vision_payload,
                    account_voice=account_voice,
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
    niche_label: str | None,
    vision_payload: dict[str, object] | None,
    account_voice: dict[str, str],
) -> dict[str, object]:
    return {
        "model": model,
        "stream": False,
        "format": "json",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You create short-form video hooks and captions. "
                    "Return only valid JSON with keys final_summary, title_options, caption_options. "
                    f"title_options must contain exactly {SMART_DRAFT_OPTION_COUNT} strings. "
                    f"caption_options must contain exactly {SMART_CAPTION_OPTION_COUNT} strings."
                ),
            },
            {
                "role": "user",
                "content": _smart_draft_prompt(
                    transcript_text=transcript_text,
                    source_title=source_title,
                    niche_label=niche_label,
                    vision_payload=vision_payload,
                    account_voice=account_voice,
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
) -> dict[str, object]:
    user_content: list[dict[str, object]] = [
        {
            "type": "text",
            "text": (
                "Study these sampled video frames as a sequence from the clip and return only valid JSON. "
                "This is especially important when the clip has no useful dialogue, is a meme, or relies on a visual reaction. "
                "Identify the visible setup, subject, action, reaction, reveal, payoff, and any readable on-screen text. "
                "Use this schema exactly: "
                '{"scene_summary":"","ocr_text":[],"main_subject":"","main_action":"","tone":"","confidence":"","hook_moments":[],"uncertainty_notes":""}. '
                "Keep values short and conservative.\n"
                f"Source title: {source_title or '(none)'}\n"
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


def _resolve_provider_order(model: str | None, api_key: str | None) -> list[tuple[str, str, str | None]]:
    providers: list[tuple[str, str, str | None]] = []
    resolved_api_key = api_key or os.environ.get("GROQ_API_KEY")
    if resolved_api_key:
        providers.append(("groq", model or os.environ.get("GROQ_MODEL") or DEFAULT_GROQ_MODEL, resolved_api_key))
    if _ollama_enabled():
        providers.append(("ollama", model or os.environ.get("OLLAMA_MODEL") or DEFAULT_OLLAMA_MODEL, None))
    return providers


def _ollama_enabled() -> bool:
    disabled = _normalize_whitespace(os.environ.get("OLLAMA_DISABLED") or "").lower()
    return disabled not in {"1", "true", "yes"}


def _groq_vision_enabled() -> bool:
    enabled = _normalize_whitespace(os.environ.get("GROQ_ENABLE_VISION") or "1").lower()
    return enabled not in {"0", "false", "no"}


def _groq_max_frames() -> int:
    raw_value = _normalize_whitespace(os.environ.get("GROQ_MAX_FRAMES") or str(DEFAULT_GROQ_MAX_FRAMES))
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
        except urllib.error.URLError as exc:
            last_error = RuntimeError(f"{provider_name} request failed: {exc.reason}")
            if attempt >= _retry_count():
                raise last_error from exc
    assert last_error is not None
    raise last_error


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


def _parse_final_drafts(response_payload: dict[str, object], *, provider_name: str) -> _ParsedDraftResponse:
    content = _extract_message_content(response_payload)
    parsed = _parse_model_json(content)
    summary = _normalize_whitespace(str(parsed.get("final_summary") or parsed.get("summary") or ""))
    title_options = _clean_options(parsed.get("title_options"))[:SMART_DRAFT_OPTION_COUNT]
    caption_options = _clean_options(parsed.get("caption_options"))[:SMART_CAPTION_OPTION_COUNT]
    if not summary or len(title_options) != SMART_DRAFT_OPTION_COUNT or len(caption_options) != SMART_CAPTION_OPTION_COUNT:
        raise RuntimeError(f"{provider_name} did not return usable smart drafts.")
    return _ParsedDraftResponse(
        summary=summary,
        title_options=title_options,
        caption_options=caption_options,
    )


def _parse_vision_payload(response_payload: dict[str, object], *, provider_name: str) -> dict[str, object]:
    content = _extract_message_content(response_payload)
    parsed = _parse_model_json(content)
    normalized = _empty_vision_payload()
    normalized["scene_summary"] = _normalize_whitespace(str(parsed.get("scene_summary") or ""))
    normalized["ocr_text"] = _clean_options(parsed.get("ocr_text"))
    normalized["main_subject"] = _normalize_whitespace(str(parsed.get("main_subject") or ""))
    normalized["main_action"] = _normalize_whitespace(str(parsed.get("main_action") or ""))
    normalized["tone"] = _normalize_whitespace(str(parsed.get("tone") or ""))
    normalized["confidence"] = _normalize_whitespace(str(parsed.get("confidence") or ""))
    normalized["hook_moments"] = _clean_options(parsed.get("hook_moments"))
    normalized["uncertainty_notes"] = _normalize_whitespace(str(parsed.get("uncertainty_notes") or ""))
    if not any(
        [
            normalized["scene_summary"],
            normalized["ocr_text"],
            normalized["main_subject"],
            normalized["main_action"],
            normalized["hook_moments"],
        ]
    ):
        raise RuntimeError(f"{provider_name} did not return usable visual extraction.")
    return normalized


def _parse_model_json(content: str) -> dict[str, object]:
    stripped = content.strip()
    if not stripped:
        raise RuntimeError("Model returned empty content.")
    stripped = re.sub(r"<think>.*?</think>", "", stripped, flags=re.DOTALL | re.IGNORECASE).strip()
    fenced_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", stripped, re.DOTALL)
    if fenced_match:
        stripped = fenced_match.group(1)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        json_object = _extract_first_json_object(stripped)
        if json_object is None:
            raise
        return json.loads(json_object)


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


def _clean_options(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    for item in value:
        text = _normalize_whitespace(str(item))
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
    summary_bits.append("Stay grounded in the visible moment and avoid inventing unsupported details.")
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
    payload["uncertainty_notes"] = "Derived from frame availability only; no structured vision model output."
    return payload


def _generate_local_fallback_drafts(
    *,
    transcript_text: str,
    source_title: str | None,
    niche_label: str | None,
    visual_summary: str | None,
    account_voice: dict[str, str],
    errors: list[str],
) -> SmartDrafts:
    base_title = _normalize_whitespace(source_title or "") or "Video Clip"
    niche_text = _normalize_whitespace(niche_label or "") or "short-form content"
    summary_signal = _normalize_whitespace(visual_summary or "") or _summarize_from_transcript(transcript_text)
    summary = summary_signal or f"A {niche_text} clip built from the current source context."
    title_options = _fallback_title_options(base_title=base_title, niche_text=niche_text, summary=summary)
    caption_options = _fallback_caption_options(
        base_title=base_title,
        niche_text=niche_text,
        summary=summary,
        transcript_text=transcript_text,
        account_voice=account_voice,
    )
    return SmartDrafts(
        summary=summary,
        title_options=title_options,
        caption_options=caption_options,
        provider_label="Local fallback",
        used_fallback=True,
        generation_meta={
            "writer_model": None,
            "vision_model": None,
            "frame_count": 0,
            "vision_used": False,
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
) -> list[str]:
    transcript_summary = _summarize_from_transcript(transcript_text)
    transcript_line = (
        f"The spoken context points to this moment clearly: {transcript_summary}."
        if transcript_summary
        else "There is limited spoken context here, so this draft leans on the source title and niche framing instead."
    )
    voice_hint = _normalize_whitespace(account_voice.get("caption_style", ""))
    options = [
        (
            f"{summary} This draft treats the clip as {niche_text}, with the focus kept on {base_title}. "
            f"{transcript_line}"
        ),
        (
            f"{base_title} stands out as a {niche_text} moment with a clear payoff. "
            "Use this as a working caption draft, then tighten the wording to match the exact beat you want to highlight."
            + (f" {voice_hint}" if voice_hint else "")
        ),
    ]
    return [_normalize_whitespace(option)[:420] for option in options][:SMART_CAPTION_OPTION_COUNT]


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
        "ocr_text": [],
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

    profile_parts: list[str] = []
    if any(keyword in niche for keyword in ("game", "gaming", "minecraft", "roblox", "fortnite")):
        profile_parts.append(
            "Use energetic gameplay language, highlight the moment, mechanic, fail, win, or payoff, "
            "and avoid sounding like a generic streamer title."
        )
    if any(keyword in niche for keyword in ("comedy", "funny", "meme")):
        profile_parts.append(
            "Lean into timing, surprise, and the funniest observable detail instead of broad hype."
        )
    if any(keyword in niche for keyword in ("tutorial", "education", "how to", "guide")):
        profile_parts.append(
            "Make the value obvious quickly and favor clarity, outcome, and practical phrasing."
        )
    if any(keyword in niche for keyword in ("motivation", "mindset", "self improvement")):
        profile_parts.append(
            "Use direct, emotionally clear language with a strong takeaway and avoid empty inspiration cliches."
        )
    if any(keyword in niche for keyword in ("podcast", "interview", "commentary", "story")):
        profile_parts.append(
            "Emphasize the sharpest idea, reveal, or quote-worthy takeaway rather than generic recap language."
        )
    if any(keyword in niche for keyword in ("animal", "pet", "nature")):
        profile_parts.append(
            "Highlight the most visible behavior, reaction, or reveal and keep the tone warm and specific."
        )

    if not profile_parts:
        profile_parts.append(
            "Use language that feels natural for the niche, emphasize the most concrete payoff, and avoid generic hooks."
        )
    return " ".join(profile_parts)


def _angle_plan(niche_label: str | None) -> str:
    niche = _normalize_whitespace(niche_label or "").lower()
    if any(keyword in niche for keyword in ("game", "gaming", "minecraft", "roblox", "fortnite")):
        return (
            "Option 1 = direct gameplay hook. "
            "Option 2 = curiosity around the mechanic, trick, or outcome. "
            "Option 3 = payoff/result framing that explains why the clip is worth watching."
        )
    if any(keyword in niche for keyword in ("comedy", "funny", "meme")):
        return (
            "Option 1 = strongest joke or reaction. "
            "Option 2 = curiosity hook around what happens next. "
            "Option 3 = observational or payoff angle that lands the reveal."
        )
    if any(keyword in niche for keyword in ("tutorial", "education", "how to", "guide")):
        return (
            "Option 1 = clearest practical value hook. "
            "Option 2 = curiosity around the method or shortcut. "
            "Option 3 = explanation or result framing that highlights the outcome."
        )
    return (
        "Option 1 = strongest direct hook. "
        "Option 2 = curiosity-driven angle. "
        "Option 3 = explanatory, observational, or payoff angle."
    )
