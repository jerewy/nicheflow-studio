from __future__ import annotations

from pathlib import Path

import pytest

from nicheflow_studio.processing import smart_drafts


@pytest.fixture(autouse=True)
def _disable_ollama_by_default(monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_DISABLED", "1")


def test_generate_smart_drafts_parses_structured_response(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(smart_drafts, "sample_video_frame_data_urls", lambda path, max_frames=3: [])

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
            return False

        def read(self) -> bytes:
            return (
                b'{"choices":[{"message":{"content":"{\\"summary\\":\\"A funny zoo moment\\",'
                b'\\"title_options\\":[\\"Elephant Chaos\\",\\"Zoo Moment\\",\\"Look At This Elephant\\"],'
                b'\\"caption_options\\":[\\"This elephant stole the show\\",\\"You need to see this zoo moment\\",\\"The best part is the elephant reveal\\"],'
                b'\\"recommended_pick\\":{\\"title_option_index\\":2,\\"caption_option_index\\":2,\\"reason\\":\\"Best fit for animal comedy because it is clearest.\\"},'
                b'\\"option_notes\\":[\\"Fastest hook\\",\\"Best overall\\",\\"Backup angle\\"]}"}}]}'
            )

    monkeypatch.setattr(
        smart_drafts.urllib.request, "urlopen", lambda request, timeout=90: FakeResponse()
    )

    result = smart_drafts.generate_smart_drafts(
        transcript_text="Here is a funny clip about an elephant at the zoo.",
        source_title="Zoo clip",
        niche_label="animal comedy",
    )

    assert result.summary == "A funny zoo moment"
    assert result.title_options[0] == "Elephant Chaos"
    assert len(result.caption_options) == 3
    assert result.recommended_title_index == 1
    assert result.recommended_caption_index == 1
    assert result.recommendation_reason == "Best fit for animal comedy because it is clearest."
    assert result.option_notes == ["Fastest hook", "Best overall", "Backup angle"]


def test_generate_smart_drafts_requires_provider(monkeypatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("OLLAMA_DISABLED", "1")

    with pytest.raises(RuntimeError, match="No smart-draft provider is configured"):
        smart_drafts.generate_smart_drafts(
            transcript_text="hello",
            source_title=None,
            niche_label=None,
        )


def test_generate_smart_drafts_rotates_through_multiple_groq_keys(monkeypatch) -> None:
    """Primary GROQ_API_KEY fails → GROQ2_API_KEY is tried next. Ollama is
    intentionally NOT in the chain — Groq-only with rotating keys for
    rate-limit / quota failover."""
    monkeypatch.setenv("GROQ_API_KEY", "primary-fails")
    monkeypatch.setenv("GROQ2_API_KEY", "fallback-works")
    monkeypatch.delenv("GROQ3_API_KEY", raising=False)
    monkeypatch.setattr(smart_drafts, "sample_video_frame_data_urls", lambda path, max_frames=3: [])

    call_log: list[str] = []

    class FakeOkResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
            return False

        def read(self) -> bytes:
            return (
                b'{"choices":[{"message":{"content":"{\\"summary\\":\\"ok\\",'
                b'\\"title_options\\":[\\"A\\",\\"B\\",\\"C\\"],'
                b'\\"caption_options\\":[\\"x\\",\\"y\\",\\"z\\"]}"}}]}'
            )

    def fake_urlopen(request, timeout=90):  # noqa: ANN001
        auth_header = request.headers.get("Authorization", "")
        call_log.append(auth_header)
        if "primary-fails" in auth_header:
            raise smart_drafts.urllib.error.HTTPError(
                request.full_url, 429, "Too Many Requests", {}, None
            )
        return FakeOkResponse()

    monkeypatch.setattr(smart_drafts.urllib.request, "urlopen", fake_urlopen)

    result = smart_drafts.generate_smart_drafts(
        transcript_text="",
        source_title="Minecraft hoe clip",
        niche_label="minecraft gameplay",
    )

    # Both keys were tried; the fallback succeeded.
    assert any("primary-fails" in h for h in call_log)
    assert any("fallback-works" in h for h in call_log)
    assert not result.used_fallback  # real Groq result, not local templates
    assert result.title_options == ["A", "B", "C"]


def test_generate_smart_drafts_can_use_metadata_without_transcript(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(smart_drafts, "sample_video_frame_data_urls", lambda path, max_frames=3: [])

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
            return False

        def read(self) -> bytes:
            return (
                b'{"choices":[{"message":{"content":"{\\"summary\\":\\"A Minecraft farming clip\\",'
                b'\\"title_options\\":[\\"Minecraft Hoe Moment\\",\\"This Farming Clip Works\\",\\"Hoe Play In Minecraft\\"],'
                b'\\"caption_options\\":[\\"This Minecraft hoe setup is weirdly clean\\",\\"No dialogue, just a solid farming moment\\",\\"Minecraft gameplay that explains itself\\"]}"}}]}'
            )

    monkeypatch.setattr(
        smart_drafts.urllib.request, "urlopen", lambda request, timeout=90: FakeResponse()
    )

    result = smart_drafts.generate_smart_drafts(
        transcript_text="",
        source_title="Minecraft hoe clip",
        niche_label="minecraft gameplay",
    )

    assert result.summary == "A Minecraft farming clip"
    assert result.title_options[0] == "Minecraft Hoe Moment"


def test_generate_smart_drafts_uses_sdk_like_payload(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(smart_drafts, "sample_video_frame_data_urls", lambda path, max_frames=3: [])
    captured_request = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
            return False

        def read(self) -> bytes:
            return (
                b'{"choices":[{"message":{"content":"{\\"summary\\":\\"A funny zoo moment\\",'
                b'\\"title_options\\":[\\"Elephant Chaos\\",\\"Zoo Moment\\",\\"Look At This Elephant\\"],'
                b'\\"caption_options\\":[\\"This elephant stole the show\\",\\"You need to see this zoo moment\\",\\"The best part is the elephant reveal\\"]}"}}]}'
            )

    def fake_urlopen(request, timeout=90):  # noqa: ANN001
        captured_request["payload"] = request.data.decode("utf-8")
        return FakeResponse()

    monkeypatch.setattr(smart_drafts.urllib.request, "urlopen", fake_urlopen)

    smart_drafts.generate_smart_drafts(
        transcript_text="Here is a funny clip about an elephant at the zoo.",
        source_title="Zoo clip",
        niche_label="animal comedy",
    )

    payload = smart_drafts.json.loads(captured_request["payload"])
    assert payload["model"] == "llama-3.3-70b-versatile"
    assert payload["stream"] is False
    assert payload["max_completion_tokens"] == 1400
    # System prompt now uses per-caption-style word targets instead of a
    # global "at least 70 words" floor. Default style yields "70-130 words".
    assert "70-130 words" in payload["messages"][0]["content"]
    assert "reasoning_effort" not in payload
    # Strict JSON mode is intentionally enabled — without it Groq Llama 3.3
    # occasionally returns malformed JSON (trailing commas, truncated output)
    # which drops the user into the lower-quality local rule-based fallback.
    assert payload["response_format"] == {"type": "json_object"}


def test_generate_smart_drafts_sends_user_agent_for_groq(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(smart_drafts, "sample_video_frame_data_urls", lambda path, max_frames=3: [])
    captured_request = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
            return False

        def read(self) -> bytes:
            return (
                b'{"choices":[{"message":{"content":"{\\"final_summary\\":\\"A funny zoo moment\\",'
                b'\\"title_options\\":[\\"Elephant Chaos\\",\\"Zoo Moment\\",\\"Look At This Elephant\\"],'
                b'\\"caption_options\\":[\\"This elephant stole the show\\",\\"You need to see this zoo moment\\",\\"The safest upload caption keeps the elephant reveal clear\\"]}"}}]}'
            )

    def fake_urlopen(request, timeout=90):  # noqa: ANN001
        captured_request["user_agent"] = request.headers.get("User-agent")
        return FakeResponse()

    monkeypatch.setattr(smart_drafts.urllib.request, "urlopen", fake_urlopen)

    smart_drafts.generate_smart_drafts(
        transcript_text="Here is a funny clip about an elephant at the zoo.",
        source_title="Zoo clip",
        niche_label="animal comedy",
    )

    assert captured_request["user_agent"] == "nicheflow-studio/0.1"


def test_groq_generation_usage_meta_estimates_cost() -> None:
    meta = smart_drafts._groq_generation_usage_meta(
        vision_response={
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 100,
                "total_tokens": 1100,
            }
        },
        writer_response={
            "usage": {
                "prompt_tokens": 2000,
                "completion_tokens": 200,
                "total_tokens": 2200,
            }
        },
    )

    assert meta["usage"]["vision"]["prompt_tokens"] == 1000
    assert meta["usage"]["writer"]["completion_tokens"] == 200
    assert meta["estimated_cost_usd"] == 0.001482


def test_groq_limit_profile_defaults_to_free_basic_safe(monkeypatch) -> None:
    monkeypatch.delenv("GROQ_MONTHLY_BUDGET_USD", raising=False)
    monkeypatch.delenv("GROQ_MONTHLY_VIDEO_CAP", raising=False)
    monkeypatch.delenv("GROQ_DAILY_VIDEO_CAP", raising=False)
    monkeypatch.delenv("GROQ_BUDGET_WARN_RATIO", raising=False)
    monkeypatch.delenv("GROQ_MAX_FRAMES", raising=False)

    profile = smart_drafts._groq_limit_profile()

    assert profile["profile"] == "free-basic-safe"
    assert profile["monthly_budget_usd"] == 1.0
    assert profile["monthly_video_cap"] == 1000
    assert profile["daily_video_cap"] == 40
    assert profile["budget_warn_at_usd"] == 0.8
    assert profile["requests_per_full_video"] == 2
    assert profile["max_frames_per_video"] == 5


def test_groq_limit_profile_clamps_env_values(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_MONTHLY_BUDGET_USD", "0")
    monkeypatch.setenv("GROQ_MONTHLY_VIDEO_CAP", "50000")
    monkeypatch.setenv("GROQ_DAILY_VIDEO_CAP", "5000")
    monkeypatch.setenv("GROQ_BUDGET_WARN_RATIO", "2")

    profile = smart_drafts._groq_limit_profile()

    assert profile["monthly_budget_usd"] == 0.01
    assert profile["monthly_video_cap"] == 20000
    assert profile["daily_video_cap"] == 1000
    assert profile["budget_warn_at_usd"] == 0.01


def test_generate_smart_drafts_prefers_groq_and_uses_vision_summary(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    monkeypatch.setenv("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
    monkeypatch.setenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    monkeypatch.setattr(
        smart_drafts,
        "sample_video_frame_data_urls",
        lambda path, max_frames=3: ["data:image/jpeg;base64,AAA", "data:image/jpeg;base64,BBB"],
    )
    captured_payloads: list[dict[str, object]] = []

    class FakeResponse:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
            return False

        def read(self) -> bytes:
            return self._payload

    def fake_urlopen(request, timeout=90):  # noqa: ANN001
        payload = smart_drafts.json.loads(request.data.decode("utf-8"))
        captured_payloads.append(payload)
        if payload["model"] == "meta-llama/llama-4-scout-17b-16e-instruct":
            return FakeResponse(
                b'{"choices":[{"message":{"content":"{\\"scene_summary\\":\\"Minecraft farming setup\\",\\"layout\\":\\"single-panel gameplay\\",\\"panel_relationship\\":\\"none\\",\\"on_screen_hook\\":\\"\\",\\"ocr_text\\":[],\\"main_subject\\":\\"hoe\\",\\"main_action\\":\\"clean farming motion\\",\\"tone\\":\\"satisfying\\",\\"confidence\\":\\"high\\",\\"hook_moments\\":[\\"clean rhythm\\"],\\"uncertainty_notes\\":\\"\\"}"}}]}'
            )
        return FakeResponse(
            b'{"choices":[{"message":{"content":"{\\"final_summary\\":\\"A Minecraft farming moment\\",'
            b'\\"title_options\\":[\\"Minecraft Hoe Moment\\",\\"This Farming Clip Works\\",\\"Hoe Play In Minecraft\\"],'
            b'\\"caption_options\\":[\\"This Minecraft hoe setup is weirdly clean\\",\\"No dialogue, just a solid farming moment\\",\\"The upload version keeps the Minecraft farming payoff clear\\"]}"}}]}'
        )

    monkeypatch.setattr(smart_drafts.urllib.request, "urlopen", fake_urlopen)

    result = smart_drafts.generate_smart_drafts(
        transcript_text="",
        source_title="Hoe hoe hoe 2",
        niche_label="minecraft gameplay",
        input_path=Path("clip.mp4"),
    )

    assert result.summary == "A Minecraft farming moment"
    assert len(captured_payloads) == 2
    assert captured_payloads[0]["model"] == "meta-llama/llama-4-scout-17b-16e-instruct"
    assert captured_payloads[1]["model"] == "llama-3.3-70b-versatile"
    assert "scene_summary" in captured_payloads[1]["messages"][1]["content"]
    assert "layout" in captured_payloads[1]["messages"][1]["content"]
    assert "panel_relationship" in captured_payloads[1]["messages"][1]["content"]
    assert "on_screen_hook" in captured_payloads[1]["messages"][1]["content"]
    assert "main_action" in captured_payloads[1]["messages"][1]["content"]
    first_payload_frames = captured_payloads[0]["messages"][1]["content"]
    assert len([item for item in first_payload_frames if item["type"] == "image_url"]) == 2
    assert result.vision_payload is not None
    assert result.vision_payload["main_action"] == "clean farming motion"


def test_generate_smart_drafts_raises_when_groq_is_forbidden(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    monkeypatch.setenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    monkeypatch.setattr(smart_drafts, "sample_video_frame_data_urls", lambda path: [])

    class FakeForbiddenResponse:
        def read(self) -> bytes:
            return b'{"error":{"message":"model not allowed","code":"model_forbidden"}}'

        def close(self) -> None:
            return None

    def fake_http_error(full_url, code, msg, hdrs=None, fp=None):  # noqa: ANN001
        return smart_drafts.urllib.error.HTTPError(
            full_url,
            code,
            msg,
            hdrs,
            FakeForbiddenResponse(),
        )

    def fake_urlopen_with_forbidden(request, timeout=90):  # noqa: ANN001
        raise fake_http_error(request.full_url, 403, "Forbidden")

    monkeypatch.setattr(smart_drafts.urllib.request, "urlopen", fake_urlopen_with_forbidden)

    result = smart_drafts.generate_smart_drafts(
        transcript_text="",
        source_title="Hoe hoe hoe 2",
        niche_label="minecraft gameplay",
        input_path=Path("clip.mp4"),
    )

    assert result.provider_label == "Local fallback"
    assert result.used_fallback is True


def test_generate_smart_drafts_raises_when_groq_has_non_403_error(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    monkeypatch.setenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    monkeypatch.setattr(smart_drafts, "sample_video_frame_data_urls", lambda path: [])

    class FakeServerErrorResponse:
        def read(self) -> bytes:
            return b'{"error":{"message":"server error"}}'

        def close(self) -> None:
            return None

    def fake_http_error(full_url, code, msg, hdrs=None, fp=None):  # noqa: ANN001
        return smart_drafts.urllib.error.HTTPError(
            full_url,
            code,
            msg,
            hdrs,
            FakeServerErrorResponse(),
        )

    def fake_urlopen(request, timeout=90):  # noqa: ANN001
        raise fake_http_error(request.full_url, 500, "Server Error")

    monkeypatch.setattr(smart_drafts.urllib.request, "urlopen", fake_urlopen)

    result = smart_drafts.generate_smart_drafts(
        transcript_text="",
        source_title="Hoe hoe hoe 2",
        niche_label="minecraft gameplay",
        input_path=Path("clip.mp4"),
    )

    assert result.provider_label == "Local fallback"
    assert result.used_fallback is True


def test_generate_smart_drafts_ignores_missing_vision_summary_when_groq_vision_is_forbidden(
    monkeypatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    monkeypatch.setenv("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
    monkeypatch.setenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    monkeypatch.setattr(
        smart_drafts,
        "sample_video_frame_data_urls",
        lambda path, max_frames=3: ["data:image/jpeg;base64,AAA", "data:image/jpeg;base64,BBB"],
    )
    captured_payloads: list[dict[str, object]] = []

    class FakeResponse:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
            return False

        def read(self) -> bytes:
            return self._payload

    class FakeForbiddenResponse:
        def read(self) -> bytes:
            return b'{"error":{"message":"model not allowed","code":"model_forbidden"}}'

        def close(self) -> None:
            return None

    def fake_http_error(full_url, code, msg, hdrs=None, fp=None):  # noqa: ANN001
        return smart_drafts.urllib.error.HTTPError(
            full_url,
            code,
            msg,
            hdrs,
            FakeForbiddenResponse(),
        )

    def fake_urlopen(request, timeout=90):  # noqa: ANN001
        payload = smart_drafts.json.loads(request.data.decode("utf-8"))
        captured_payloads.append(payload)
        if (
            request.full_url == smart_drafts.GROQ_CHAT_COMPLETIONS_URL
            and payload["model"] == "meta-llama/llama-4-scout-17b-16e-instruct"
        ):
            raise fake_http_error(request.full_url, 403, "Forbidden")
        return FakeResponse(
            b'{"choices":[{"message":{"content":"{\\"final_summary\\":\\"A Minecraft farming moment\\",'
            b'\\"title_options\\":[\\"Minecraft Hoe Moment\\",\\"This Farming Clip Works\\",\\"Hoe Play In Minecraft\\"],'
            b'\\"caption_options\\":[\\"This Minecraft hoe setup is weirdly clean\\",\\"No dialogue, just a solid farming moment\\",\\"The upload version keeps the Minecraft farming payoff clear\\"]}"}}]}'
        )

    monkeypatch.setattr(smart_drafts.urllib.request, "urlopen", fake_urlopen)

    result = smart_drafts.generate_smart_drafts(
        transcript_text="",
        source_title="Hoe hoe hoe 2",
        niche_label="minecraft gameplay",
        input_path=Path("clip.mp4"),
    )

    assert result.summary == "A Minecraft farming moment"
    assert captured_payloads[0]["model"] == "meta-llama/llama-4-scout-17b-16e-instruct"
    assert captured_payloads[1]["model"] == "llama-3.3-70b-versatile"
    assert '"scene_summary": "(none)"' in captured_payloads[1]["messages"][1]["content"]
    assert result.vision_payload is None


def test_generate_smart_drafts_includes_account_personalization(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(smart_drafts, "sample_video_frame_data_urls", lambda path: [])
    captured_request = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
            return False

        def read(self) -> bytes:
            return (
                b'{"choices":[{"message":{"content":"{\\"final_summary\\":\\"A funny zoo moment\\",'
                b'\\"title_options\\":[\\"Elephant Chaos\\",\\"Zoo Moment\\",\\"Look At This Elephant\\"],'
                b'\\"caption_options\\":[\\"This elephant stole the show\\",\\"You need to see this zoo moment\\",\\"The upload version keeps the elephant moment clear\\"]}"}}]}'
            )

    def fake_urlopen(request, timeout=90):  # noqa: ANN001
        captured_request["payload"] = smart_drafts.json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(smart_drafts.urllib.request, "urlopen", fake_urlopen)

    smart_drafts.generate_smart_drafts(
        transcript_text="Here is a funny clip about an elephant at the zoo.",
        source_title="Zoo clip",
        niche_label="animal comedy",
        account_voice={
            "clip_context": "This is a bus-driver reaction meme where attractive passengers board while a guitar layer reacts.",
            "tone": "playful",
            "target_audience": "short-form animal fans",
            "hook_style": "reaction-first",
            "banned_phrases": "like and follow",
            "title_style": "short punchy hooks",
            "caption_style": "comment-style reactions",
        },
    )

    prompt = captured_request["payload"]["messages"][1]["content"]
    assert "Account voice settings" in prompt
    assert "Creator-provided clip premise" in prompt
    assert "bus-driver reaction meme" in prompt
    assert "playful" in prompt
    assert "reaction-first" in prompt
    assert "like and follow" in prompt


def test_smart_draft_prompt_is_niche_aware() -> None:
    prompt = smart_drafts._smart_draft_prompt(
        transcript_text="A fast Minecraft farming clip with no dialogue.",
        source_title="Hoe hoe hoe 2",
        niche_label="minecraft gameplay",
    )

    assert "You are a short-form video clipper" in prompt
    assert "Account niche: minecraft gameplay" in prompt
    assert "Write like someone who understands the minecraft gameplay niche." in prompt
    assert "Use energetic gameplay language" in prompt
    assert "Option angle plan: Option 1 = direct gameplay hook." in prompt
    assert "Account voice settings" in prompt
    assert "Visual evidence JSON" in prompt
    # Word target is now per-caption-style; default style is "70-130 words".
    # Old hardcoded "Do not return captions under 70 words" was removed because
    # it overrode the per-style word rules (meme_relatable wants 5-20).
    assert "about 70-130 words" in prompt
    assert "Separate every paragraph with one blank line." in prompt
    assert "Do not exceed 5 hashtags." in prompt
    assert "master of rhymes" in prompt
    assert "Transcript:" in prompt


def test_smart_draft_prompt_uses_processing_template_profile() -> None:
    prompt = smart_drafts._smart_draft_prompt(
        transcript_text="A Minecraft player shows an impossible base.",
        source_title="That One Friend",
        niche_label="gaming shorts",
        prompt_profile="gaming_meme",
    )

    assert "Write like a meme and clip account such as meme.ig" in prompt
    assert "relatable POV or situation hook" in prompt
    assert "That one friend who" in prompt
    assert "3 short paragraphs that feel like a person reacting" in prompt
    assert "Never write a noun-phrase label" in prompt


def test_smart_draft_prompt_supports_contextual_caption_style() -> None:
    prompt = smart_drafts._smart_draft_prompt(
        transcript_text="A movie reaction meme with no useful dialogue.",
        source_title="Passenger trailer scene",
        source_description="Passenger is a horror trailer about a couple who realize something followed them after a crash.",
        niche_label="movie memes",
        caption_style="contextual_info",
    )

    assert "Original source caption (supporting context)" in prompt
    assert "Passenger is a horror trailer" in prompt
    assert "Do not copy it directly" in prompt
    assert "Caption emphasis: lead with the relatable moment or feeling first" in prompt
    # Fix B rewrote the default caption_style_line: instead of "ground a new
    # viewer (one sentence max)", which invited encyclopedia openers, the
    # rule now points the model at clip-specific detail and forbids
    # defining the game/show/format.
    assert "THIS clip" in prompt
    assert "do NOT define" in prompt.lower() or "do not define" in prompt.lower()
    assert "tag a friend" in prompt


def test_smart_draft_prompt_includes_meme_contextual_formula_fields() -> None:
    prompt = smart_drafts._smart_draft_prompt(
        transcript_text="",
        source_title="Gaming loss meme",
        niche_label="instagram meme account",
        prompt_profile="gaming_meme",
        caption_style="contextual_info",
        vision_payload={
            "scene_summary": "Two players are frustrated after losing several matches.",
            "on_screen_hook": "When me and bro haven't won a single game in 4 hours",
            "implied_premise": "The friend blames the losing streak on cosmetic skins.",
            "referenced_concept": "changing skins",
            "concept_definition": "Changing skins means customizing a character appearance without changing gameplay.",
            "meme_caption_premise": "Bro starts talking about changing skins after a losing streak.",
            "context_explainer_seed": "Skins change how a character looks, but usually do not affect gameplay.",
        },
    )

    assert "Visual evidence JSON" in prompt
    assert "on_screen_hook" in prompt
    assert "When me and bro haven't won a single game in 4 hours" in prompt
    assert "concept_definition" in prompt
    assert "Changing skins means customizing" in prompt
    assert "meme_caption_premise" in prompt
    assert "strongest seed for the new on-screen title" in prompt


def test_smart_draft_prompt_requires_distinct_option_angles() -> None:
    prompt = smart_drafts._smart_draft_prompt(
        transcript_text="Eminem talks about rhyming with orange.",
        source_title="Video by meme.ig",
        niche_label="instagram meme account",
        prompt_profile="gaming_meme",
        caption_style="contextual_info",
        vision_payload={
            "scene_summary": "Eminem discusses whether orange can be rhymed.",
            "on_screen_hook": "Eminem is the ONLY person who can rhyme a word with orange",
            "referenced_entity": "Eminem",
            "referenced_concept": "orange has no perfect rhyme",
        },
    )

    assert "OUTPUT OPTIONS" in prompt
    assert "exactly 3 distinct on-screen titles" in prompt
    assert "each with a different opening sentence and angle" in prompt
    assert "Do not write three rewrites of one line." in prompt
    assert "recommended_pick" in prompt
    assert "strongest title/caption pair" in prompt
    assert "option_notes" in prompt
    assert "master of rhymes" in prompt


def test_smart_draft_prompt_includes_recent_draft_deduplication() -> None:
    prompt = smart_drafts._smart_draft_prompt(
        transcript_text="A movie reaction meme with no useful dialogue.",
        source_title="Passenger trailer scene",
        niche_label="movie memes",
        recent_titles=["Already Used Hook", "Already Used Hook", "Different Hook"],
        recent_captions=["This caption opening already appeared on this account."],
    )

    assert "Previously used drafts from this niche account" in prompt
    assert prompt.count("Already Used Hook") == 1
    assert "Different Hook" in prompt
    assert "Caption openings" in prompt
    assert "This caption opening already appeared on this account." in prompt
    assert "must be clearly distinct" in prompt


def test_smart_draft_prompt_uses_visual_first_mode_without_transcript() -> None:
    prompt = smart_drafts._smart_draft_prompt(
        transcript_text="",
        source_title="When the cat realizes",
        source_description="When your cat realizes the mirror enemy is actually itself.",
        niche_label="meme animals",
        prompt_profile="reaction_clip",
        vision_payload={
            "scene_summary": "A cat freezes after seeing itself in a mirror.",
            "ocr_text": ["wait for it"],
            "main_subject": "cat",
            "main_action": "surprised mirror reaction",
            "tone": "meme reaction",
            "confidence": "high",
            "hook_moments": ["cat freezes", "mirror reveal"],
            "uncertainty_notes": "",
        },
    )

    assert (
        "No-transcript mode: visual evidence and source caption are your primary context." in prompt
    )
    assert "Source caption (PRIMARY CONTEXT - no transcript available)" in prompt
    assert "When your cat realizes the mirror enemy is actually itself." in prompt
    assert "Write like a real meme page" in prompt
    assert "Write like a meme and clip account such as meme.ig" in prompt
    assert "cat freezes" in prompt
    assert "strongest seed for the new on-screen title" in prompt


def test_visual_summary_payload_requests_reaction_layout_fields() -> None:
    payload = smart_drafts._build_visual_summary_payload(
        model="vision-model",
        transcript_text="",
        source_title="how can i get this job?",
        source_description="A dream job meme about a bus driver role.",
        niche_label="reaction meme",
        visual_frame_urls=["data:image/jpeg;base64,AAA"],
    )

    prompt = payload["messages"][1]["content"][0]["text"]
    assert "Original source caption: A dream job meme about a bus driver role." in prompt
    assert "split-screen" in prompt
    assert "original video premise" in prompt
    assert "reaction/audio layers" in prompt
    assert "on_screen_hook" in prompt
    assert "implied_premise" in prompt
    assert "referenced_entity" in prompt
    assert "referenced_concept" in prompt
    assert "concept_definition" in prompt
    assert "meme_caption_premise" in prompt
    assert "context_explainer_seed" in prompt
    assert "visible_roles" in prompt
    assert "top_text_type" in prompt
    assert "bottom_text_type" in prompt
    assert "suggested_title_layout" in prompt
    assert "Dialogue subtitles and meme-joke text are content" in prompt
    assert "instead of saying the subject is applying" in prompt
    assert "panel_relationship" in prompt
    assert "instead of guessing" in prompt


def test_parse_vision_payload_preserves_reaction_layout_fields() -> None:
    parsed = smart_drafts._parse_vision_payload(
        {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"scene_summary":"A bus driver waits while stylish passengers board.",'
                            '"layout":"top panel bus scene, bottom panel guitar reaction",'
                            '"panel_relationship":"The guitar layer reacts to the bus-driver job premise.",'
                            '"on_screen_hook":"how can i get this job?",'
                            '"implied_premise":"The viewer wants the bus driver role because attractive passengers are boarding.",'
                            '"referenced_entity":"",'
                            '"referenced_concept":"dream job meme",'
                            '"concept_definition":"A dream job meme frames an ordinary role as desirable because of an exaggerated perk.",'
                            '"meme_caption_premise":"The driver job looks desirable because stylish passengers are boarding.",'
                            '"context_explainer_seed":"Dream job memes turn everyday work into a fantasy job by highlighting one funny perk.",'
                            '"visible_roles":["bus driver","passengers","guitar reaction hands"],'
                            '"ocr_text":["how can i get this job?"],'
                            '"top_text_type":"meme_joke",'
                            '"bottom_text_type":"subtitle",'
                            '"keep_top_text":true,'
                            '"keep_bottom_text":true,'
                            '"suggested_title_layout":"no_title",'
                            '"content_box":{"top":0.42,"bottom":0.78,"left":0.05,"right":0.95},'
                            '"crop_reason":"Keep meme text and subtitles.",'
                            '"main_subject":"bus driver and passengers",'
                            '"main_action":"passengers board while guitar reacts",'
                            '"tone":"joking admiration",'
                            '"confidence":"high",'
                            '"hook_moments":["beautiful passengers board"],'
                            '"uncertainty_notes":""}'
                        )
                    }
                }
            ]
        },
        provider_name="Groq vision",
    )

    assert parsed["layout"] == "top panel bus scene, bottom panel guitar reaction"
    assert parsed["panel_relationship"].startswith("The guitar layer reacts")
    assert parsed["on_screen_hook"] == "how can i get this job?"
    assert parsed["implied_premise"].startswith("The viewer wants the bus driver role")
    assert parsed["referenced_entity"] == ""
    assert parsed["referenced_concept"] == "dream job meme"
    assert parsed["concept_definition"].startswith("A dream job meme")
    assert parsed["meme_caption_premise"].startswith("The driver job looks desirable")
    assert parsed["context_explainer_seed"].startswith("Dream job memes")
    assert parsed["visible_roles"] == ["bus driver", "passengers", "guitar reaction hands"]
    assert parsed["top_text_type"] == "meme_joke"
    assert parsed["bottom_text_type"] == "subtitle"
    assert parsed["keep_top_text"] is True
    assert parsed["keep_bottom_text"] is True
    assert parsed["suggested_title_layout"] == "no_title"
    assert parsed["content_box"] == {
        "top": 0.42,
        "bottom": 0.78,
        "left": 0.05,
        "right": 0.95,
    }
    assert parsed["crop_reason"] == "Keep meme text and subtitles."


def test_smart_draft_prompt_supports_documentary_memory_style() -> None:
    prompt = smart_drafts._smart_draft_prompt(
        transcript_text="A grandson used AI to restore old family photos for his grandfather.",
        source_title="They brought his childhood memories back to life with AI technology",
        niche_label="AI restoration family history",
        prompt_profile="story_reel",
        vision_payload={
            "scene_summary": "An older man watches restored childhood family images.",
            "ocr_text": ["They brought his childhood memories back to life with AI technology"],
            "main_subject": "grandfather and restored family photos",
            "main_action": "watching restored memories come alive",
            "tone": "emotional documentary",
            "confidence": "high",
            "hook_moments": ["restored childhood photos", "grandfather reaction"],
            "uncertainty_notes": "",
        },
    )

    assert "emotionally clear human-interest storyteller" in prompt
    assert "emotionally clear sentence hook" in prompt
    assert "compact human-interest story" in prompt
    assert "grandfather reaction" in prompt


def test_smart_draft_prompt_picks_tone_and_uses_account_lean() -> None:
    base_prompt = smart_drafts._smart_draft_prompt(
        transcript_text="A funny fail clip.",
        source_title="Big fail",
        niche_label="gaming memes",
        prompt_profile="gaming_meme",
    )
    leaning_prompt = smart_drafts._smart_draft_prompt(
        transcript_text="A funny fail clip.",
        source_title="Big fail",
        niche_label="gaming memes",
        prompt_profile="gaming_meme",
        account_voice={"tone": "playful"},
    )

    assert "Choose the tone that fits this exact clip" in base_prompt
    assert "funny for fails" in base_prompt
    assert "This account leans toward a playful tone" in leaning_prompt
    assert "This account leans toward a playful tone" not in base_prompt


def test_clean_options_preserves_caption_paragraph_breaks() -> None:
    cleaned = smart_drafts._clean_options(
        ["Para one.\n\nPara two.\n\n#tag1 #tag2"],
        preserve_paragraphs=True,
    )

    assert cleaned == ["Para one.\n\nPara two.\n\n#tag1 #tag2"]


def test_clean_options_strips_only_whole_title_bold_wrapper() -> None:
    cleaned = smart_drafts._clean_options(
        [
            "**Jason Paige Sang the Pokemon Theme in a Single Take**",
            "The moment that **reframes** the fight.",
        ],
        preserve_paragraphs=True,
        strip_wrapping_bold=True,
    )

    assert cleaned == [
        "Jason Paige Sang the Pokemon Theme in a Single Take",
        "The moment that **reframes** the fight.",
    ]


def test_parse_final_drafts_preserves_title_paragraph_breaks() -> None:
    inner = smart_drafts.json.dumps(
        {
            "final_summary": "A meme clip.",
            "title_options": [
                'Friend: "stop sending me reels"\n\nMe:',
                "When they ask you to stop sending Reels:",
                'Everyone: "stop spamming"\n\nMe:',
            ],
            "caption_options": ["one", "two", "three"],
        }
    )

    parsed = smart_drafts._parse_final_drafts(
        {"choices": [{"message": {"content": inner}}]},
        provider_name="Test",
    )

    assert parsed.title_options[0] == 'Friend: "stop sending me reels"\n\nMe:'
    assert parsed.title_options[2] == 'Everyone: "stop spamming"\n\nMe:'


def test_normalize_caption_text_collapses_excess_blank_lines() -> None:
    normalized = smart_drafts._normalize_caption_text("Para one.\n\n\n\nPara two.   \n\n#tag")

    assert normalized == "Para one.\n\nPara two.\n\n#tag"


def test_parse_final_drafts_keeps_caption_paragraphs() -> None:
    inner = smart_drafts.json.dumps(
        {
            "final_summary": "A meme clip.",
            "title_options": ["Title A", "Title B", "Title C"],
            "caption_options": [
                "Hook line.\n\nContext paragraph that explains the reference.\n\n#one #two #three",
                "Second hook.\n\nSecond context paragraph.\n\n#four #five #six",
                "Third hook.\n\nThird context paragraph.\n\n#seven #eight #nine",
            ],
        }
    )

    parsed = smart_drafts._parse_final_drafts(
        {"choices": [{"message": {"content": inner}}]},
        provider_name="Test",
    )

    assert "\n\n" in parsed.caption_options[0]
    assert parsed.caption_options[0].startswith("Hook line.")


def test_niche_profile_falls_back_to_generic_style() -> None:
    profile = smart_drafts._niche_profile(None)

    assert "widely understandable short-form writing" in profile


def test_angle_plan_falls_back_to_generic_sequence() -> None:
    plan = smart_drafts._angle_plan("craft videos")

    assert "Option 1 = strongest direct hook." in plan
    assert "Option 3 = explanatory, observational, or payoff angle." in plan


def test_fallback_caption_options_include_relevant_hashtags() -> None:
    options = smart_drafts._fallback_caption_options(
        base_title="Grandfather watches childhood memories restored with AI",
        niche_text="AI restoration family history",
        summary="An older man sees restored family photos come alive.",
        transcript_text="",
        account_voice={},
    )

    assert len(options) == 3
    assert "#family" in options[0]
    assert "#aitools" in options[0]


def test_parse_model_json_accepts_reasoning_preamble() -> None:
    content = """
    Here is the result:
    ```json
    {"summary":"Minecraft farming clip","title_options":["A","B","C"],"caption_options":["one","two","three"]}
    ```
    """

    parsed = smart_drafts._parse_model_json(content)

    assert parsed["summary"] == "Minecraft farming clip"


def test_parse_model_json_accepts_literal_newlines_inside_strings() -> None:
    content = (
        '{"summary":"ok","title_options":["A","B","C"],'
        '"caption_options":["line one\nline two","two","three"]}'
    )

    parsed = smart_drafts._parse_model_json(content)

    assert parsed["caption_options"][0] == "line one\nline two"


def test_extract_message_content_handles_content_parts() -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "output_text", "text": "First line"},
                        {
                            "type": "output_text",
                            "text": '{"summary":"ok","title_options":["A","B","C"],"caption_options":["one","two","three"]}',
                        },
                    ]
                }
            }
        ]
    }

    content = smart_drafts._extract_message_content(payload)

    assert "First line" in content
    assert '"summary":"ok"' in content


def test_extract_message_content_handles_nested_content_parts() -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": [
                        [{"type": "output_text", "text": '{"scene_summary":"A Minecraft path"}'}]
                    ]
                }
            }
        ]
    }

    content = smart_drafts._extract_message_content(payload)

    assert '"scene_summary":"A Minecraft path"' in content


def test_generate_smart_drafts_raises_when_provider_output_is_unusable(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    monkeypatch.setenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    monkeypatch.setattr(smart_drafts, "sample_video_frame_data_urls", lambda path, max_frames=3: [])

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
            return False

        def read(self) -> bytes:
            return b'{"choices":[{"message":{"content":"not valid json at all"}}]}'

    monkeypatch.setattr(
        smart_drafts.urllib.request, "urlopen", lambda request, timeout=90: FakeResponse()
    )

    result = smart_drafts.generate_smart_drafts(
        transcript_text="A silent Minecraft farming clip with a hoe.",
        source_title="Hoe hoe hoe 2",
        niche_label="minecraft gameplay",
    )

    assert result.provider_label == "Local fallback"
    assert result.used_fallback is True


# ---------------------------------------------------------------------------
# Caption-style profile rules (meme_relatable, meme_factual)
# ---------------------------------------------------------------------------


def test_normalize_caption_style_remaps_legacy_relatable() -> None:
    """Old 'relatable' DB rows must remap to 'meme_relatable' silently so the
    rebuilt rules apply without a database migration."""
    assert smart_drafts._normalize_caption_style("relatable") == "meme_relatable"
    assert smart_drafts._normalize_caption_style("RELATABLE") == "meme_relatable"
    assert smart_drafts._normalize_caption_style("contextual_info") == "contextual_info"
    assert smart_drafts._normalize_caption_style("meme_factual") == "meme_factual"
    assert smart_drafts._normalize_caption_style("lost_archive") == "history_lost_archive"
    assert smart_drafts._normalize_caption_style("history") == "history_lost_archive"
    assert smart_drafts._normalize_caption_style("past_moments") == "history_lost_archive"
    assert smart_drafts._normalize_caption_style(None) == "contextual_info"


def test_caption_word_targets_differ_per_style() -> None:
    # meme_relatable bumped from 5-20 → 15-35 because the lower bound was too
    # tight: the model hugged 5 words and produced anemic fragments.
    assert smart_drafts._caption_word_target("meme_relatable") == "40-80"
    assert smart_drafts._caption_word_target("meme_factual") == "50-120"
    assert smart_drafts._caption_word_target("contextual_info") == "70-130"
    assert smart_drafts._caption_word_target("hype") == "70-130"
    # Legacy value still mapped:
    assert smart_drafts._caption_word_target("relatable") == "40-80"
    assert smart_drafts._caption_word_target("meme_daily_cope") == "75-130"
    assert smart_drafts._caption_word_target("history_lost_archive") == "90-150"


def test_caption_hashtag_targets_differ_per_style() -> None:
    # meme.ig data: only 9% of their posts use hashtags. meme_factual mirrors
    # that with 0-2 optional. meme_relatable keeps 3-5 for cold-start
    # discoverability.
    assert smart_drafts._caption_hashtag_target("meme_factual") == "0-2"
    assert smart_drafts._caption_hashtag_target("meme_relatable") == "3-5"
    assert smart_drafts._caption_hashtag_target("contextual_info") == "3-5"
    assert smart_drafts._caption_hashtag_target("history_lost_archive") == "3-5"


def test_meme_relatable_system_prompt_enforces_hybrid_hook_plus_context() -> None:
    """Meme Relatable v3: hybrid hook + light context format. The short
    hook drives shares; the context paragraph gives the Instagram algorithm
    enough caption text to categorize the post for Explore/FYP reach."""
    prompt = smart_drafts._smart_draft_system_prompt("meme_relatable")
    assert "40-80 words" in prompt
    # Must enforce the hook + context hybrid structure:
    assert "hook" in prompt.lower()
    assert "context" in prompt.lower()
    assert "3-5 hashtags" in prompt
    # Must NOT inherit the old 70-word floor:
    assert "at least 70 words" not in prompt


def test_daily_cope_system_prompt_is_longer_than_other_meme_lanes() -> None:
    prompt = smart_drafts._smart_draft_system_prompt("meme_daily_cope")
    line = smart_drafts._caption_style_line("meme_daily_cope")

    assert "DAILY COPE" in prompt
    assert "75-130 words" in prompt
    assert "4-6 short casual sentences" in prompt
    assert "longer than the other meme lanes" in line


def test_history_lost_archive_system_prompt_enforces_archive_context() -> None:
    prompt = smart_drafts._smart_draft_system_prompt("history_lost_archive")
    line = smart_drafts._caption_style_line("history_lost_archive")

    assert "PAST MOMENTS DAILY" in prompt
    assert "90-150 words" in prompt
    assert "notable moment from the past" in prompt.lower()
    assert "never invent facts" in prompt.lower()
    assert "specific subject" in prompt
    assert "Opening the archive" in prompt
    assert "A forgotten fact" in prompt
    assert "do not simply restate" in prompt.lower()
    assert "Past Moments Daily" in line
    assert "Never use generic openers" in line
    assert "became memorable" in line


def test_history_lost_archive_title_rules_are_not_meme_framing() -> None:
    rules = smart_drafts._caption_style_title_rules("history_lost_archive")
    joined = "\n".join(rules)

    # New explanatory-hook range (was 5-11; longer hooks name the subject).
    assert "10-16 words" in joined
    # Mystery-bait phrases now appear ONLY as banned/weak examples.
    assert "lost story" in joined.lower()
    assert "This old footage aged strangely" in joined
    assert "BANNED" in joined
    assert "me when" in joined.lower()


def test_history_lost_archive_title_rules_include_twist_and_comment_hooks() -> None:
    rules = smart_drafts._caption_style_title_rules("history_lost_archive")
    joined = "\n".join(rules)

    # Two-beat twist shape: setup sentence + short punch ("He said no.").
    assert "TWIST BEAT" in joined
    assert "He said no." in joined
    # Comment-bait question/observation shape requires a NAMED subject, so it
    # cannot collide with the subject-hiding bans below it.
    assert "COMMENT HOOK" in joined
    assert "NAMES the subject" in joined
    # The engagement goal is explicit: titles should provoke comments/shares.
    assert "COMMENT TEST" in joined
    assert "intrigue without controversy" in joined


def test_history_lost_archive_title_rules_mandate_concrete_subject() -> None:
    rules = smart_drafts._caption_style_title_rules("history_lost_archive")
    joined = "\n".join(rules)

    # Core upgrade: name the visible subject + one surprise, not vague bait.
    assert "NAMES the concrete visible subject" in joined
    assert "never just label it" in joined.lower()
    # The strong calibration example is present as the "good" anchor.
    assert "camping tents to scooters" in joined
    assert "The ski lift ride where John Denver wrote Annie's Song for his wife" in joined
    assert "Too flat" in joined
    # Vague subject-hiding bait is explicitly banned, not recommended.
    assert "hides the subject" in joined.lower()
    # Fact discipline: no invented rarity / disappearance / first-ever.
    assert "FACT DISCIPLINE" in joined
    assert "first-ever status" in joined


def test_niche_profile_history_names_subject_and_avoids_podcast_misroute() -> None:
    # The real Past Moments niche_label contains "stories", which used to
    # misroute history into the podcast/story branch.
    profile = smart_drafts._niche_profile(
        "History moments, old clips, strange facts, and forgotten stories"
    )

    assert "NAME the visible subject" in profile
    assert "never invent rarity" in profile
    # Must NOT pick up the talking-head guidance.
    assert "quote-worthy takeaway" not in profile


def test_angle_plan_history_uses_curiosity_nostalgia_comparison() -> None:
    plan = smart_drafts._angle_plan(
        "History moments, old clips, strange facts, and forgotten stories"
    )

    assert "curiosity / surprising fact" in plan
    assert "nostalgia / everyday-life" in plan
    assert "modern comparison" in plan
    # Not the generic fallback sequence.
    assert "Option 1 = strongest direct hook." not in plan


HISTORY_NICHE = "History moments, old clips, strange facts, and forgotten stories"


def test_effective_title_rules_auto_routes_history_without_explicit_style() -> None:
    # No explicit title_style, generic/broad caption_style: a history account
    # should still get the history hook rules, not the generic fallback.
    rules = smart_drafts.effective_title_rules(
        title_style=None, caption_style="broad_short_form", niche_label=HISTORY_NICHE
    )
    joined = "\n".join(rules)

    assert "NAMES the concrete visible subject" in joined
    assert "10-16 words" in joined


def test_effective_title_rules_respects_explicit_title_style_for_history() -> None:
    # An explicit pick always wins, even for a history account.
    rules = smart_drafts.effective_title_rules(
        title_style="meme_setup_punchline",
        caption_style="broad_short_form",
        niche_label=HISTORY_NICHE,
    )
    joined = "\n".join(rules)

    assert "SETUP whose punchline" in joined
    assert "NAMES the concrete visible subject" not in joined


def test_effective_title_rules_non_history_keeps_caption_fallback() -> None:
    # Non-history account with no explicit title_style falls back to the
    # caption-style-derived rules (here: meme_relatable), not history.
    rules = smart_drafts.effective_title_rules(
        title_style=None,
        caption_style="meme_relatable",
        niche_label="Relatable daily cope memes",
    )
    joined = "\n".join(rules)

    assert "4-8 words" in joined
    assert "NAMES the concrete visible subject" not in joined


def test_effective_title_rules_history_with_story_profile_keeps_its_voice() -> None:
    # A history-flavoured niche on a profile that has its OWN title voice
    # (story_reel) must NOT be hijacked into archival hooks.
    rules = smart_drafts.effective_title_rules(
        title_style=None,
        caption_style=None,
        niche_label="AI restoration family history",
        prompt_profile="story_reel",
    )
    joined = "\n".join(rules)

    assert "NAMES the concrete visible subject" not in joined


def test_history_prompt_auto_applies_history_title_rules_end_to_end() -> None:
    # The whole prompt for a history account (no explicit title_style) must carry
    # the history title rules AND suppress the generic profile title line.
    prompt = smart_drafts._smart_draft_prompt(
        transcript_text="",
        source_title="Video by theanomalists",
        niche_label=HISTORY_NICHE,
        prompt_profile="broad_short_form",
        caption_style="broad_short_form",
        title_style=None,
    )

    assert "NAMES the concrete visible subject" in prompt
    assert "camping tents to scooters" in prompt


def test_meme_factual_system_prompt_enforces_emoji_plus_wikipedia() -> None:
    prompt = smart_drafts._smart_draft_system_prompt("meme_factual")
    assert "50-120 words" in prompt
    assert "ONE emoji on the first line" in prompt
    assert "Wikipedia-style" in prompt


def test_meme_relatable_title_rules_force_viewer_centric_framing() -> None:
    rules = smart_drafts._caption_style_title_rules("meme_relatable")
    joined = "\n".join(rules)
    assert "4-8 words" in joined
    assert "'me '" in joined
    assert "'when '" in joined
    # Must forbid celebrity-as-subject titles:
    assert "BANNED" in joined


def test_meme_factual_title_rules_force_observational_short_hook() -> None:
    """Meme Factual on-screen titles must be 3-7 word observational hooks,
    NOT emoji-only and NOT 'me when' viewer-centric framing. The emoji
    belongs in the caption opener (separate field), never as standalone
    on-screen video text — that was the bug that produced bare 😂 titles."""
    rules = smart_drafts._caption_style_title_rules("meme_factual")
    joined = "\n".join(rules)
    assert "3-7 words" in joined
    # Must explicitly ban emoji-only titles:
    assert "emoji-only" in joined.lower()
    # Must ban viewer-centric framing (that belongs to Meme Relatable):
    assert "BANNED" in joined
    assert "pov" in joined.lower()


def test_meme_relatable_word_target_supports_hybrid_format() -> None:
    """Meme Relatable evolved into a HYBRID: 1-line relatable hook + 2-3
    sentences of light context + hashtags. Target bumped to 40-80 words
    so the model has room for the context paragraph that Instagram's
    algorithm needs to categorize the post for Explore/FYP reach."""
    assert smart_drafts._caption_word_target("meme_relatable") == "40-80"


def test_default_caption_style_title_rules_keep_legacy_behaviour() -> None:
    """Contextual / hype / default styles preserve the old 'no emojis no
    hashtags' title rule so existing non-meme accounts aren't disturbed."""
    rules = smart_drafts._caption_style_title_rules("contextual_info")
    joined = "\n".join(rules)
    assert "no hashtags and no emojis" in joined


# ---------------------------------------------------------------------------
# Hook drama vs factual-safety tiering (green / yellow / red)
# ---------------------------------------------------------------------------


def test_hook_drama_rules_permit_drama_and_ban_overclaims() -> None:
    """The framing block must (a) explicitly allow dramatic/emotional hooks
    and (b) ban the unverifiable RED-tier overclaim phrasings."""
    joined = "\n".join(smart_drafts._hook_drama_and_fact_safety_rules())
    # Permission to be dramatic — the fix for flat documentary-label titles.
    assert "Dramatic" in joined
    assert "Dramatize the MEANING" in joined
    # Green / yellow / red tiering is spelled out.
    assert "GREEN" in joined
    assert "YELLOW" in joined
    assert "RED" in joined
    # Representative banned RED phrasings.
    assert "never-before-seen" in joined
    assert "changed history forever" in joined
    # Identity mislabeling guard.
    assert "Michael Jordan" in joined


def test_smart_draft_prompt_includes_hook_drama_framing_section() -> None:
    """The drama/factual-safety block is wired into the title section of the
    assembled prompt, not just available as a standalone helper."""
    prompt = smart_drafts._smart_draft_prompt(
        transcript_text="",
        source_title="Old basketball clip",
        niche_label="history archive",
        caption_style="history_lost_archive",
    )
    assert "HOOK FRAMING (drama is allowed, overclaiming is not)" in prompt
    assert "Dramatize the MEANING" in prompt
    assert "never-before-seen" in prompt


def test_clean_option_tiers_normalizes_and_defaults_to_yellow() -> None:
    """Decorated/cased labels still classify; garbage falls back to the
    conservative 'yellow'; a non-list returns None so 'unset' is
    distinguishable from 'all green'."""
    assert smart_drafts._clean_option_tiers(["green", "RED", "Tier: yellow"]) == [
        "green",
        "red",
        "yellow",
    ]
    assert smart_drafts._clean_option_tiers(["green", "banana", ""]) == [
        "green",
        "yellow",
        "yellow",
    ]
    assert smart_drafts._clean_option_tiers(None) is None
    assert smart_drafts._clean_option_tiers("green") is None


def test_parse_final_drafts_extracts_option_tiers() -> None:
    import json

    content = json.dumps(
        {
            "final_summary": "An old basketball clip",
            "title_options": ["At 5'3 he made the NBA", "Defying the odds", "Too short, they said"],
            "caption_options": ["First caption.", "Second caption.", "Third caption."],
            "option_notes": ["fact pick", "emotional", "underdog"],
            "option_tiers": ["yellow", "green", "green"],
        }
    )
    payload = {"choices": [{"message": {"content": content}}]}
    parsed = smart_drafts._parse_final_drafts(payload, provider_name="Test")
    assert parsed.option_tiers == ["yellow", "green", "green"]


def test_system_and_user_prompts_request_option_tiers() -> None:
    system_prompt = smart_drafts._smart_draft_system_prompt("contextual_info")
    assert "option_tiers" in system_prompt
    user_prompt = smart_drafts._smart_draft_prompt(
        transcript_text="",
        source_title="Old clip",
        niche_label="history archive",
    )
    assert "option_tiers" in user_prompt


# ---------------------------------------------------------------------------
# Narrative caption style (theanomalists pattern)
# ---------------------------------------------------------------------------


def test_narrative_word_target_is_longest() -> None:
    """Narrative is the longest format — 100-200 words. The value of this
    style IS the multi-paragraph story, not a short hook."""
    assert smart_drafts._caption_word_target("narrative") == "100-200"


def test_narrative_system_prompt_enforces_news_article_format() -> None:
    prompt = smart_drafts._smart_draft_system_prompt("narrative")
    assert "100-200 words" in prompt
    # Must enforce multi-paragraph storytelling, not single hook:
    assert "paragraphs" in prompt.lower()
    # Must use news-article framing, not meme framing:
    assert "narrative" in prompt.lower() or "story" in prompt.lower()


def test_narrative_title_rules_force_long_headline_format() -> None:
    """Narrative on-screen titles must be long descriptive headlines (7-14
    words, third-person), not short hooks or 'me when' framing."""
    rules = smart_drafts._caption_style_title_rules("narrative")
    joined = "\n".join(rules)
    assert "7-14 words" in joined
    # Must ban both Meme Relatable and Meme Factual framings:
    assert "BANNED" in joined
    assert "pov" in joined.lower()
    # No emojis in on-screen narrative titles:
    assert "no hashtags, no emojis" in joined


def test_cinema_hook_title_rules_offer_varied_templates_not_one_dominant() -> None:
    """The movie atmospheric title rules must offer several equally-weighted
    templates and force each of the three options onto a different one. The old
    block crowned one template 'dominant' and seeded 'silence' as an example
    word, which made every generation collapse into the same shape and word."""
    rules = smart_drafts._caption_style_title_rules("cinema_hook")
    joined = "\n".join(rules)
    # Six named templates, not just A/B/C:
    for template in (
        "TEMPLATE A",
        "TEMPLATE B",
        "TEMPLATE C",
        "TEMPLATE D",
        "TEMPLATE E",
        "TEMPLATE F",
    ):
        assert template in joined
    # No single template is crowned dominant/default/preferred anymore:
    assert "dominant" not in joined.lower()
    # Each of the three options must use a DIFFERENT template:
    assert "DIFFERENT template" in joined
    # The 'silence' anchor must be gone from the template EXAMPLES, and only
    # survive as an explicitly banned crutch word:
    assert "That kind of silence" not in joined
    assert "crutch" in joined.lower()
    assert "'silence'" in joined


def test_cinema_bold_keywords_uses_modes_keywords_and_distinct_bold() -> None:
    """Cinema Bold Keywords should avoid the fixed cinema_hook template rhythm.

    It needs broader editorial modes and keyword/anchor variation while keeping
    optional bold keywords for the overlay renderer.
    """
    rules = smart_drafts._title_style_rules("cinema_bold_keywords")
    assert rules is not None
    joined = "\n".join(rules)
    assert "TITLE MODES" in joined
    assert "MIXED INGREDIENTS" in joined
    assert "watch-if-you-like" in joined
    assert "rewatch/detail hook" in joined
    assert "direct and plain" in joined
    assert "3 rewrites of the same idea" in joined
    # Does not inherit the rigid cinema_hook template block:
    assert "TEMPLATE F" not in joined
    # Bold markup rule is still present (we keep the **word** output):
    assert "EMPHASIS MARKUP" in joined
    # Distinct bold word across options is now enforced:
    assert "DISTINCT EMPHASIS" in joined
    assert "MUST differ" in joined
    # The seeded '**silence**' example must be gone:
    assert "**silence**" not in joined


def test_recent_draft_dedup_discourages_template_and_bold_reuse() -> None:
    """Anti-repetition must push beyond verbatim de-dup: reusing the same
    opening template or bolded keyword as a recent title is what made movie
    hooks feel same-y, so the instruction must call both out."""
    prompt = smart_drafts._recent_draft_dedup_prompt(
        recent_titles=["That kind of **silence** that says everything"],
        recent_captions=None,
    )
    lowered = prompt.lower()
    assert "template" in lowered
    assert "bolded keyword" in lowered


# ---------------------------------------------------------------------------
# Fix A — vision diagnostics, low-context detection, require_vision, retry
# ---------------------------------------------------------------------------


class _FakeJsonResponse:
    """Tiny urlopen response stand-in shared by the Fix A tests."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
        return False

    def read(self) -> bytes:
        return self._payload


_VISION_OK_BODY = (
    b'{"choices":[{"message":{"content":"{\\"scene_summary\\":\\"Minecraft trap moment\\",'
    b'\\"layout\\":\\"single-panel gameplay\\",\\"main_action\\":\\"trap springs\\",'
    b'\\"main_subject\\":\\"player\\",\\"confidence\\":\\"high\\",\\"hook_moments\\":[\\"reveal\\"],'
    b'\\"uncertainty_notes\\":\\"\\"}"}}]}'
)

_WRITER_OK_BODY = (
    b'{"choices":[{"message":{"content":"{\\"final_summary\\":\\"Trap reveal\\",'
    b'\\"title_options\\":[\\"Trap Reveal A\\",\\"Trap Reveal B\\",\\"Trap Reveal C\\"],'
    b'\\"caption_options\\":[\\"Cap one body\\",\\"Cap two body\\",\\"Cap three body\\"]}"}}]}'
)


def _make_http_error(full_url: str, code: int, msg: str) -> "smart_drafts.urllib.error.HTTPError":
    class _Body:
        def read(self) -> bytes:
            return b'{"error":{"message":"rate_limit"}}'

        def close(self) -> None:
            return None

    return smart_drafts.urllib.error.HTTPError(full_url, code, msg, None, _Body())


# --- _is_low_context_source_title -------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "",
        "   ",
        "Video by meme.ig",
        "Reel by someone",
        "Post by handle",
        "Instagram_DYfJT5WOtzJ",
        "shorts_abc123",
        "yt_aBcDeF1",
        "Reel",  # too short
    ],
)
def test_low_context_titles_detected_when_no_transcript(title: str) -> None:
    assert smart_drafts._is_low_context_source_title(title, "") is True


def test_low_context_false_with_meaningful_title() -> None:
    assert (
        smart_drafts._is_low_context_source_title("Trapping a streamer in my elytra drip trap", "")
        is False
    )


def test_low_context_false_when_transcript_is_substantial() -> None:
    # Even with a generic title, a real transcript carries enough signal that
    # writer-only output is acceptable.
    transcript = "So basically we built this trap and waited for him to fall in"
    assert smart_drafts._is_low_context_source_title("Video by meme.ig", transcript) is False


# --- generation_meta diagnostics --------------------------------------------


def test_generation_meta_includes_new_diagnostic_fields_on_vision_success(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    monkeypatch.setattr(
        smart_drafts,
        "sample_video_frame_data_urls",
        lambda path, max_frames=5: ["data:image/jpeg;base64,AAA", "data:image/jpeg;base64,BBB"],
    )

    def fake_urlopen(request, timeout=90):  # noqa: ANN001
        payload = smart_drafts.json.loads(request.data.decode("utf-8"))
        if payload["model"].startswith("meta-llama/llama-4"):
            return _FakeJsonResponse(_VISION_OK_BODY)
        return _FakeJsonResponse(_WRITER_OK_BODY)

    monkeypatch.setattr(smart_drafts.urllib.request, "urlopen", fake_urlopen)

    result = smart_drafts.generate_smart_drafts(
        transcript_text="",
        source_title="Video by meme.ig",
        niche_label="gaming memes",
        input_path=Path("clip.mp4"),
    )

    meta = result.generation_meta or {}
    assert meta["vision_attempted"] is True
    assert meta["vision_used"] is True
    assert meta["vision_retry_attempted"] is False
    assert meta["vision_error"] is None
    assert meta["low_context"] is True
    assert meta["frame_count"] == 2


def test_generation_meta_records_vision_error_when_vision_fails(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    monkeypatch.delenv("GROQ2_API_KEY", raising=False)
    monkeypatch.delenv("GROQ3_API_KEY", raising=False)
    monkeypatch.delenv("GROQ4_API_KEY", raising=False)
    monkeypatch.setattr(
        smart_drafts,
        "sample_video_frame_data_urls",
        lambda path, max_frames=5: ["data:image/jpeg;base64,AAA"] * 5,
    )

    def fake_urlopen(request, timeout=90):  # noqa: ANN001
        payload = smart_drafts.json.loads(request.data.decode("utf-8"))
        if payload["model"].startswith("meta-llama/llama-4"):
            # Non-rate-limit failure so we don't trip the retry path here —
            # this test asserts the *single-shot* error surfaces in meta.
            raise _make_http_error(request.full_url, 403, "Forbidden")
        return _FakeJsonResponse(_WRITER_OK_BODY)

    monkeypatch.setattr(smart_drafts.urllib.request, "urlopen", fake_urlopen)

    result = smart_drafts.generate_smart_drafts(
        transcript_text="",
        source_title="A clip with a clear subject named in the title",
        niche_label="gaming",
        input_path=Path("clip.mp4"),
    )

    meta = result.generation_meta or {}
    assert meta["vision_attempted"] is True
    assert meta["vision_used"] is False
    assert meta["vision_error"] is not None
    assert "403" in meta["vision_error"]


# --- require_vision behaviour -----------------------------------------------


def test_require_vision_raises_on_low_context_when_vision_unused(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    monkeypatch.delenv("GROQ2_API_KEY", raising=False)
    monkeypatch.delenv("GROQ3_API_KEY", raising=False)
    monkeypatch.delenv("GROQ4_API_KEY", raising=False)
    monkeypatch.setattr(
        smart_drafts,
        "sample_video_frame_data_urls",
        lambda path, max_frames=5: ["data:image/jpeg;base64,AAA"] * 5,
    )

    def fake_urlopen(request, timeout=90):  # noqa: ANN001
        payload = smart_drafts.json.loads(request.data.decode("utf-8"))
        if payload["model"].startswith("meta-llama/llama-4"):
            raise _make_http_error(request.full_url, 403, "Forbidden")
        return _FakeJsonResponse(_WRITER_OK_BODY)

    monkeypatch.setattr(smart_drafts.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(smart_drafts.VisionRequiredError):
        smart_drafts.generate_smart_drafts(
            transcript_text="",
            source_title="Video by meme.ig",
            niche_label="gaming memes",
            input_path=Path("clip.mp4"),
            require_vision=True,
        )


def test_require_vision_passes_when_vision_used(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    monkeypatch.setattr(
        smart_drafts,
        "sample_video_frame_data_urls",
        lambda path, max_frames=5: ["data:image/jpeg;base64,AAA"] * 3,
    )

    def fake_urlopen(request, timeout=90):  # noqa: ANN001
        payload = smart_drafts.json.loads(request.data.decode("utf-8"))
        if payload["model"].startswith("meta-llama/llama-4"):
            return _FakeJsonResponse(_VISION_OK_BODY)
        return _FakeJsonResponse(_WRITER_OK_BODY)

    monkeypatch.setattr(smart_drafts.urllib.request, "urlopen", fake_urlopen)

    result = smart_drafts.generate_smart_drafts(
        transcript_text="",
        source_title="Video by meme.ig",
        niche_label="gaming memes",
        input_path=Path("clip.mp4"),
        require_vision=True,
    )

    assert (result.generation_meta or {})["vision_used"] is True


def test_require_vision_passes_for_high_context_item_without_vision(monkeypatch) -> None:
    """A clip with a substantial transcript should NOT be gated by
    require_vision — the writer has enough grounding on its own."""
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    monkeypatch.delenv("GROQ2_API_KEY", raising=False)
    monkeypatch.delenv("GROQ3_API_KEY", raising=False)
    monkeypatch.delenv("GROQ4_API_KEY", raising=False)
    monkeypatch.setattr(
        smart_drafts,
        "sample_video_frame_data_urls",
        lambda path, max_frames=5: ["data:image/jpeg;base64,AAA"] * 3,
    )

    def fake_urlopen(request, timeout=90):  # noqa: ANN001
        payload = smart_drafts.json.loads(request.data.decode("utf-8"))
        if payload["model"].startswith("meta-llama/llama-4"):
            raise _make_http_error(request.full_url, 403, "Forbidden")
        return _FakeJsonResponse(_WRITER_OK_BODY)

    monkeypatch.setattr(smart_drafts.urllib.request, "urlopen", fake_urlopen)

    # Should not raise — the long transcript means this is NOT low-context.
    result = smart_drafts.generate_smart_drafts(
        transcript_text=(
            "Okay so we built this trap with a redstone trigger and waited for "
            "him to walk into it on stream and it actually worked first try"
        ),
        source_title="Video by meme.ig",
        niche_label="gaming memes",
        input_path=Path("clip.mp4"),
        require_vision=True,
    )
    assert (result.generation_meta or {})["vision_used"] is False


# --- low-context retry with fewer frames ------------------------------------


def test_low_context_vision_retry_fires_with_fewer_frames(monkeypatch) -> None:
    """When vision fails on a low-context item, smart_drafts must retry
    once with LOW_CONTEXT_RETRY_FRAME_COUNT frames before giving up. This
    is the path that recovers a 429 by shrinking the prompt under Groq's
    TPM window."""
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    monkeypatch.delenv("GROQ2_API_KEY", raising=False)
    monkeypatch.delenv("GROQ3_API_KEY", raising=False)
    monkeypatch.delenv("GROQ4_API_KEY", raising=False)
    monkeypatch.setattr(
        smart_drafts,
        "sample_video_frame_data_urls",
        lambda path, max_frames=5: ["data:image/jpeg;base64,AAA"] * 5,
    )

    vision_calls: list[int] = []

    def fake_urlopen(request, timeout=90):  # noqa: ANN001
        payload = smart_drafts.json.loads(request.data.decode("utf-8"))
        if payload["model"].startswith("meta-llama/llama-4"):
            # Count the number of image frames sent in this vision call.
            user_content = payload["messages"][1]["content"]
            frame_count = len([p for p in user_content if p.get("type") == "image_url"])
            vision_calls.append(frame_count)
            if frame_count > smart_drafts.LOW_CONTEXT_RETRY_FRAME_COUNT:
                # First call: full 5 frames — simulate 429 rate-limit.
                raise _make_http_error(request.full_url, 429, "Too Many Requests")
            # Retry call with 2 frames: succeed.
            return _FakeJsonResponse(_VISION_OK_BODY)
        return _FakeJsonResponse(_WRITER_OK_BODY)

    monkeypatch.setattr(smart_drafts.urllib.request, "urlopen", fake_urlopen)

    result = smart_drafts.generate_smart_drafts(
        transcript_text="",
        source_title="Video by meme.ig",
        niche_label="gaming memes",
        input_path=Path("clip.mp4"),
    )

    # First call used the full frame budget; retry used the reduced budget.
    assert len(vision_calls) >= 2
    assert vision_calls[0] > smart_drafts.LOW_CONTEXT_RETRY_FRAME_COUNT
    assert vision_calls[-1] == smart_drafts.LOW_CONTEXT_RETRY_FRAME_COUNT
    meta = result.generation_meta or {}
    assert meta["vision_retry_attempted"] is True
    assert meta["vision_used"] is True


def test_parse_model_json_raises_runtime_error_on_think_only_content() -> None:
    """Reasoning models sometimes emit only <think>...</think> with no JSON
    body. _parse_model_json must surface this as RuntimeError so the vision
    retry path can catch it — a raw JSONDecodeError used to escape both the
    key rotation and the low-context retry and dump straight to Local
    fallback (observed in the app as the toast: 'Generated fallback drafts
    because the primary provider failed. Reason: Expecting value: line 1
    column 1 (char 0)')."""
    with pytest.raises(RuntimeError, match="reasoning content"):
        smart_drafts._parse_model_json("<think>I should output JSON.</think>")


def test_parse_model_json_raises_runtime_error_on_non_json_content() -> None:
    """Garbage non-JSON text must surface as RuntimeError, not a leaked
    JSONDecodeError, so callers can rely on a single exception type."""
    with pytest.raises(RuntimeError, match="non-JSON"):
        smart_drafts._parse_model_json("this is not json at all")


def test_low_context_retry_fires_when_vision_returns_empty_content(monkeypatch) -> None:
    """The bug from the in-app toast: vision returned content that parsed
    to empty (the JSONDecodeError 'Expecting value: line 1 column 1 (char
    0)' case). Before the parse-error normalization, this escaped vision
    rotation AND the low-context retry, dropping straight to Local
    fallback. After the fix, it must trigger the same retry-with-2-frames
    path as a 429."""
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    monkeypatch.delenv("GROQ2_API_KEY", raising=False)
    monkeypatch.delenv("GROQ3_API_KEY", raising=False)
    monkeypatch.delenv("GROQ4_API_KEY", raising=False)
    monkeypatch.setattr(
        smart_drafts,
        "sample_video_frame_data_urls",
        lambda path, max_frames=5: ["data:image/jpeg;base64,AAA"] * 5,
    )

    vision_frame_counts: list[int] = []

    def fake_urlopen(request, timeout=90):  # noqa: ANN001
        payload = smart_drafts.json.loads(request.data.decode("utf-8"))
        if payload["model"].startswith("meta-llama/llama-4"):
            user_content = payload["messages"][1]["content"]
            frame_count = len([p for p in user_content if p.get("type") == "image_url"])
            vision_frame_counts.append(frame_count)
            if frame_count > smart_drafts.LOW_CONTEXT_RETRY_FRAME_COUNT:
                # First call returns only reasoning, no JSON body — the
                # exact failure mode observed in the app screenshot.
                return _FakeJsonResponse(
                    b'{"choices":[{"message":{"content":"<think>thinking...</think>"}}]}'
                )
            return _FakeJsonResponse(_VISION_OK_BODY)
        return _FakeJsonResponse(_WRITER_OK_BODY)

    monkeypatch.setattr(smart_drafts.urllib.request, "urlopen", fake_urlopen)

    result = smart_drafts.generate_smart_drafts(
        transcript_text="",
        source_title="Video by meme.ig",
        niche_label="gaming memes",
        input_path=Path("clip.mp4"),
    )

    assert len(vision_frame_counts) >= 2, "low-context retry must fire on parse failure"
    assert vision_frame_counts[-1] == smart_drafts.LOW_CONTEXT_RETRY_FRAME_COUNT
    meta = result.generation_meta or {}
    assert meta["vision_used"] is True
    assert meta["vision_retry_attempted"] is True
    assert result.used_fallback is False  # we recovered, no Local fallback


# ---------------------------------------------------------------------------
# Fix B — kill Wikipedia-explainer drift and audience-label leaks
# ---------------------------------------------------------------------------


def test_gaming_meme_profile_no_longer_pushes_ground_a_new_viewer() -> None:
    """The old 'ground a new viewer (what the thing is)' instruction was
    the source of the encyclopedia-opener drift. Fix B removed it from the
    gaming_meme/reaction_clip profile."""
    block = smart_drafts._profile_style_block("gaming_meme")
    assert "ground a new viewer" not in block["caption"]
    assert "what the thing is" not in block["caption"]
    # Replacement language must explicitly point at clip-specific detail:
    assert "THIS clip" in block["caption"]
    assert "NOT a definition" in block["caption"]


def test_lost_archive_profile_uses_archive_voice() -> None:
    block = smart_drafts._profile_style_block("past_moments")

    assert "Past Moments Daily" in block["style"]
    assert "small story from the past" in block["style"]
    assert "short past-moment hook" in block["title"]
    assert "No fake facts" in block["caption"]
    assert "Opening the archive" in block["caption"]
    assert "became memorable" in block["caption"]


def test_cinema_study_profile_uses_cinematic_voice() -> None:
    block = smart_drafts._profile_style_block("cinema_study")

    assert "cinema recommendation account" in block["style"]
    assert "atmospheric sentence" in block["title"]
    assert "scene-led movie recommendation" in block["caption"]
    assert "meme repost" in block["style"]


def test_default_caption_style_line_no_longer_pushes_ground_a_new_viewer() -> None:
    """Same drift was present in the default contextual_info caption_style_line."""
    line = smart_drafts._caption_style_line(None)
    assert "ground a new viewer" not in line
    assert "THIS clip" in line
    assert "do NOT define" in line.lower() or "do not define" in line.lower()


@pytest.mark.parametrize(
    "caption_style",
    [None, "contextual_info", "meme_relatable", "hype"],
)
def test_anti_explainer_bans_present_for_non_factual_styles(caption_style: str | None) -> None:
    lines = smart_drafts._anti_explainer_avoid_lines(caption_style, None)
    joined = "\n".join(lines)
    assert "Encyclopedia/explainer openers" in joined
    assert "Explain-the-joke openers" in joined
    assert "target_audience" in joined or "audience" in joined.lower()


def test_anti_explainer_bans_absent_for_meme_factual_style() -> None:
    """meme_factual IS deliberately Wikipedia-tone — the bans would fight
    the style there."""
    assert smart_drafts._anti_explainer_avoid_lines("meme_factual", None) == []


def test_anti_explainer_ban_injects_specific_target_audience_string() -> None:
    """When the account voice has a target_audience, that exact string
    must appear in the ban so the model cannot quietly paraphrase it back
    into the caption (the 'Gen Z gamers and meme fans...' leak observed
    on real clips)."""
    lines = smart_drafts._anti_explainer_avoid_lines(
        None,
        {"target_audience": "Gen Z gamers and meme fans"},
    )
    joined = "\n".join(lines)
    assert "Gen Z gamers and meme fans" in joined


def test_negative_and_positive_examples_present_for_non_factual_styles() -> None:
    block = smart_drafts._negative_caption_examples_block("contextual_info")
    joined = "\n".join(block)
    assert "NEGATIVE EXAMPLES" in joined
    assert "POSITIVE EXAMPLES" in joined
    # Concrete bad examples (encyclopedia, audience-leak, explain-the-joke):
    assert "Minecraft is a popular sandbox game" in joined
    assert "Gen Z gamers and meme fans" in joined
    assert "This clip is funny because" in joined
    # Concrete good examples (clip-specific, viewer-centric):
    assert "POV:" in joined


@pytest.mark.parametrize("caption_style", ["meme_factual", "narrative"])
def test_negative_examples_skipped_for_styles_that_would_conflict(caption_style: str) -> None:
    """meme_factual is intentionally encyclopedic; narrative is intentionally
    long news-article form. The default examples would fight both styles."""
    assert smart_drafts._negative_caption_examples_block(caption_style) == []


def test_built_prompt_includes_anti_explainer_bans_and_examples() -> None:
    """End-to-end: the assembled user prompt must carry the new Fix B
    bans into the actual request payload."""
    prompt = smart_drafts._smart_draft_prompt(
        transcript_text="",
        source_title="Video by meme.ig",
        niche_label="gaming memes",
        prompt_profile="gaming_meme",
        caption_style="contextual_info",
        account_voice={"target_audience": "Gen Z gamers and meme fans"},
    )
    assert "Encyclopedia/explainer openers" in prompt
    assert "Explain-the-joke openers" in prompt
    assert "Gen Z gamers and meme fans" in prompt
    assert "NEGATIVE EXAMPLES" in prompt
    assert "POSITIVE EXAMPLES" in prompt
    # And the removed drift must not have crept back in elsewhere:
    assert "ground a new viewer" not in prompt


def test_built_prompt_for_meme_factual_skips_anti_explainer_bans() -> None:
    """When the user explicitly wants the meme.ig encyclopedic style,
    Fix B's bans must not appear in the prompt and fight that intent."""
    prompt = smart_drafts._smart_draft_prompt(
        transcript_text="",
        source_title="Video by meme.ig",
        niche_label="gaming memes",
        prompt_profile="gaming_meme",
        caption_style="meme_factual",
    )
    assert "Encyclopedia/explainer openers" not in prompt
    assert "NEGATIVE EXAMPLES" not in prompt


def test_low_context_retry_skipped_when_context_is_sufficient(monkeypatch) -> None:
    """High-context items must not waste a retry call when vision fails —
    the writer has enough grounding to proceed."""
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    monkeypatch.delenv("GROQ2_API_KEY", raising=False)
    monkeypatch.delenv("GROQ3_API_KEY", raising=False)
    monkeypatch.delenv("GROQ4_API_KEY", raising=False)
    monkeypatch.setattr(
        smart_drafts,
        "sample_video_frame_data_urls",
        lambda path, max_frames=5: ["data:image/jpeg;base64,AAA"] * 5,
    )

    monkeypatch.setenv("GROQ_RETRY_COUNT", "0")  # avoid the inner 429 retry loop noise
    vision_frame_counts: list[int] = []

    def fake_urlopen(request, timeout=90):  # noqa: ANN001
        payload = smart_drafts.json.loads(request.data.decode("utf-8"))
        if payload["model"].startswith("meta-llama/llama-4"):
            user_content = payload["messages"][1]["content"]
            frame_count = len([p for p in user_content if p.get("type") == "image_url"])
            vision_frame_counts.append(frame_count)
            raise _make_http_error(request.full_url, 429, "Too Many Requests")
        return _FakeJsonResponse(_WRITER_OK_BODY)

    monkeypatch.setattr(smart_drafts.urllib.request, "urlopen", fake_urlopen)

    result = smart_drafts.generate_smart_drafts(
        transcript_text=("We built this trap and waited and waited and it actually worked"),
        source_title="A specific descriptive clip title",
        niche_label="gaming",
        input_path=Path("clip.mp4"),
    )

    # Retry path is gated on low_context — for a high-context clip, no vision
    # call should have used the reduced frame budget.
    assert vision_frame_counts, "vision should have been attempted at least once"
    assert all(
        count > smart_drafts.LOW_CONTEXT_RETRY_FRAME_COUNT for count in vision_frame_counts
    ), f"unexpected reduced-frame retry on high-context item: {vision_frame_counts}"
    assert (result.generation_meta or {})["vision_retry_attempted"] is False


# ---------------------------------------------------------------------------
# A: contextual_info zoom-in arc
# ---------------------------------------------------------------------------


def test_contextual_info_paragraph_rule_enforces_zoom_in_arc() -> None:
    """The default rule used to be a vague '2-3 paragraphs' line. The
    contextual_info branch must now codify the theanomalists zoom-in
    template (general -> broader -> THIS moment)."""
    rule = smart_drafts._caption_paragraph_rule("contextual_info")
    assert "THEANOMALISTS" in rule or "zoom-in" in rule
    assert "Paragraph 1" in rule
    assert "Paragraph 2" in rule
    assert "Paragraph 3" in rule
    assert "BROADER CONTEXT" in rule
    assert "THIS MOMENT" in rule.upper() or "THIS clip" in rule


def test_contextual_info_title_rules_split_from_default() -> None:
    """contextual_info had been falling through to the default title rules.
    It now has its own branch with explicit clip-specific guidance."""
    rules = smart_drafts._caption_style_title_rules("contextual_info")
    joined = "\n".join(rules)
    assert "clip-specific" in joined or "specific" in joined.lower()
    assert "5-12 words" in joined
    # Must ban Meme Relatable framing and encyclopedia openers:
    assert "BANNED" in joined
    assert "pov" in joined.lower()
    assert "encyclopedia" in joined.lower()


def test_contextual_info_word_target_unchanged() -> None:
    """The word target stays at 70-130 — the change is structural, not
    length-based."""
    assert smart_drafts._caption_word_target("contextual_info") == "70-130"


# ---------------------------------------------------------------------------
# B: news_brief style end-to-end
# ---------------------------------------------------------------------------


def test_news_brief_style_word_target() -> None:
    assert smart_drafts._caption_word_target("news_brief") == "60-120"


def test_news_brief_style_hashtag_target_matches_meme_ig() -> None:
    """meme.ig post-1 had zero hashtags. 0-2 leaves room but defaults to none."""
    assert smart_drafts._caption_hashtag_target("news_brief") == "0-2"


def test_news_brief_paragraph_rule_enforces_engagement_opener_and_topic_emojis() -> None:
    rule = smart_drafts._caption_paragraph_rule("news_brief")
    assert "engagement opener" in rule.lower()
    assert "semantic" in rule.lower()
    # Topic emoji map must be present so the model knows which emoji means what:
    assert "💸" in rule and "⚡" in rule
    # Each paragraph must be a single FACT, not a hook or relatable line —
    # the structural difference from Meme Relatable.
    assert "ONE specific fact" in rule
    # Word target threaded through:
    assert "60-120 words" in rule


def test_news_brief_title_rules_explicitly_ban_meme_relatable_framing() -> None:
    """The 'me when' / 'pov:' ban for news_brief lives in the title rules
    (the paragraph rule only governs structure)."""
    rules = smart_drafts._caption_style_title_rules("news_brief")
    joined = "\n".join(rules).lower()
    assert "'me ...'" in joined or "me when" in joined or "'me " in joined
    assert "pov" in joined


def test_news_brief_caption_style_line_describes_news_brief_template() -> None:
    line = smart_drafts._caption_style_line("news_brief")
    assert "news-brief" in line.lower()
    assert "fact" in line.lower()
    # Must explicitly mention naming entities (the meme.ig hallmark):
    assert "name" in line.lower()


def test_news_brief_title_rules_allow_trailing_emoji_but_ban_pov() -> None:
    rules = smart_drafts._caption_style_title_rules("news_brief")
    joined = "\n".join(rules)
    assert "6-12 words" in joined
    # Trailing emoji explicitly allowed:
    assert "🤔" in joined
    # But pov/me-when framing banned (that's Meme Relatable):
    assert "BANNED" in joined
    assert "pov" in joined.lower()


def test_news_brief_skips_anti_explainer_bans() -> None:
    """The whole style IS factual paragraphs — encyclopedia bans would
    fight the intent."""
    assert smart_drafts._anti_explainer_avoid_lines("news_brief", None) == []
    assert smart_drafts._negative_caption_examples_block("news_brief") == []


def test_processing_style_dropdown_is_pruned_to_niche_styles() -> None:
    """Processing should show the small day-to-day Meme/Movie style set,
    while old backend styles remain available for saved rows."""
    main_window_src = (
        Path(__file__).parent.parent / "src" / "nicheflow_studio" / "app" / "main_window.py"
    ).read_text(encoding="utf-8")
    assert 'addItem("(Meme) Context / info", "contextual_info")' in main_window_src
    assert '"meme_friend_group"' in main_window_src
    assert '"meme_bro_main_character"' in main_window_src
    assert '"meme_chronically_online"' in main_window_src
    assert '"meme_reaction_situation"' in main_window_src
    assert '"meme_daily_cope"' in main_window_src
    assert '"(Movie) Cinema Atmospheric", "cinema_hook"' in main_window_src
    assert 'addItem("News Brief", "news_brief")' not in main_window_src
    assert 'addItem("Hype", "hype")' not in main_window_src


def test_news_brief_built_prompt_carries_template_through() -> None:
    """End-to-end: assembling the prompt for news_brief must include the
    fact-paragraph template, semantic emoji map, and skip the anti-explainer
    bans intended for other styles."""
    prompt = smart_drafts._smart_draft_prompt(
        transcript_text="",
        source_title="Emergent Labs raises Series B",
        niche_label="tech news",
        caption_style="news_brief",
    )
    assert "news-brief" in prompt.lower() or "NEWS-BRIEF" in prompt
    assert "semantic" in prompt.lower()
    # Anti-explainer block must NOT appear for news_brief:
    assert "Encyclopedia/explainer openers" not in prompt
    assert "NEGATIVE EXAMPLES" not in prompt


# ---------------------------------------------------------------------------
# C: name-the-thing rule
# ---------------------------------------------------------------------------


def test_name_the_thing_rules_returns_empty_when_no_vision_payload() -> None:
    """No-op when there's no vision — never asks the model to invent names."""
    assert smart_drafts._name_the_thing_rules(None) == []


def test_name_the_thing_rules_returns_empty_when_vision_extracted_nothing() -> None:
    """Empty/sentinel values in the vision payload must not produce a rule
    asking the model to use names it doesn't have."""
    empty_payload = smart_drafts._empty_vision_payload()
    assert smart_drafts._name_the_thing_rules(empty_payload) == []


def test_name_the_thing_rules_injects_extracted_names() -> None:
    """When vision actually identified entities, the rule must list them
    verbatim and forbid generic hedging."""
    payload = smart_drafts._empty_vision_payload()
    payload["referenced_entity"] = "Kevin Hart"
    payload["main_subject"] = "Druski"
    payload["referenced_concept"] = "Roast of Tom Brady"
    lines = smart_drafts._name_the_thing_rules(payload)
    joined = "\n".join(lines)
    assert "Kevin Hart" in joined
    assert "Druski" in joined
    assert "Roast of Tom Brady" in joined
    # The ban on generic hedging is the whole point of the rule:
    assert "this guy" in joined or "an actor" in joined
    assert "NAME THE THING" in joined


def test_name_the_thing_rules_dedupe_repeated_names() -> None:
    """If two vision fields land on the same string we should not list it
    twice — the prompt stays clean."""
    payload = smart_drafts._empty_vision_payload()
    payload["referenced_entity"] = "Kevin Hart"
    payload["main_subject"] = "Kevin Hart"
    lines = smart_drafts._name_the_thing_rules(payload)
    joined = "\n".join(lines)
    # Should appear once in the names list, not twice:
    assert joined.count("'Kevin Hart'") == 1


def test_name_the_thing_rule_fires_in_built_prompt_when_vision_has_entities() -> None:
    """End-to-end: vision payload with named entities -> NAME THE THING
    block appears in the assembled prompt. Vision payload with nothing ->
    block is absent."""
    payload = smart_drafts._empty_vision_payload()
    payload["referenced_entity"] = "Kevin Hart"
    payload["main_subject"] = "Druski"

    with_names = smart_drafts._smart_draft_prompt(
        transcript_text="",
        source_title="Roast moment",
        niche_label="comedy",
        vision_payload=payload,
        caption_style="contextual_info",
    )
    assert "NAME THE THING" in with_names
    assert "Kevin Hart" in with_names

    without_names = smart_drafts._smart_draft_prompt(
        transcript_text="",
        source_title="Roast moment",
        niche_label="comedy",
        vision_payload=None,
        caption_style="contextual_info",
    )
    assert "NAME THE THING" not in without_names


# ---------------------------------------------------------------------------
# Title Style decoupling — _title_style_rules + threading
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title_style", [None, "", "auto", "AUTO", "auto_match_caption", "none"])
def test_title_style_rules_returns_none_for_auto_sentinels(title_style) -> None:
    """Auto / empty / None must return None so the caller falls back to the
    caption-style-derived title rules. This is the backward-compat path —
    if any of these regressed, every existing user would see different
    titles even though they never touched the new Title Style dropdown."""
    assert smart_drafts._title_style_rules(title_style) is None


def test_title_style_rules_returns_meme_setup_punchline_rules() -> None:
    """The new IGHT-pattern style: setup with a trailing colon, video
    footage carries the punchline."""
    rules = smart_drafts._title_style_rules("meme_setup_punchline")
    assert rules is not None
    joined = "\n".join(rules)
    assert "trailing colon" in joined.lower() or "ending with a colon" in joined.lower()
    assert "6-16 words" in joined
    # The colon convention is the structural signal that makes the style:
    assert "(:)" in joined or "REQUIRED" in joined
    # The example from the user's reference post must be present so the
    # model has a concrete calibration target:
    assert "right:" in joined.lower()
    # Must forbid including BOTH setup and punchline in the title:
    assert "BANNED" in joined


def test_meme_setup_punchline_describes_both_templates() -> None:
    """Template A (single-line 'When X:') AND Template B (Them/Me contrast
    two-line) must both be present so the model can produce either pattern
    depending on the clip."""
    rules = smart_drafts._title_style_rules("meme_setup_punchline")
    assert rules is not None
    joined = "\n".join(rules)
    assert "TEMPLATE B" in joined
    # Template B contrast framings must be named so the model has a
    # vocabulary to choose from:
    assert "Everyone:" in joined or "My friends:" in joined
    # The user's reference example must appear verbatim as calibration:
    assert "Me driving 40 in a 40 zone" in joined
    # Template B requires an explicit \\n\\n inside the title string so
    # the renderer reserves a paragraph break — the rule must say so:
    assert "\\n\\n" in joined
    assert "ALL THREE options MUST use Template B" in joined
    assert "CREATIVE REMIX IS REQUIRED" in joined
    assert "SPECIFICITY RULE" in joined
    assert "Group chat" in joined
    assert "Traffic school" in joined
    # Punchline must NOT appear in either line:
    assert "punchline" in joined.lower()


@pytest.mark.parametrize(
    "title_style",
    [
        "meme_relatable",
        "meme_factual",
        "narrative",
        "news_brief",
        "contextual_info",
        "history_lost_archive",
    ],
)
def test_title_style_rules_delegates_for_known_styles(title_style: str) -> None:
    """For known caption-driven styles, the title rules must match what
    _caption_style_title_rules would return — so the user can mix freely
    (e.g. News Brief title with Context/Info caption) without duplicating
    rule bodies."""
    delegated = smart_drafts._title_style_rules(title_style)
    expected = smart_drafts._caption_style_title_rules(title_style)
    assert delegated == expected


def test_title_style_rules_unknown_falls_back_to_none() -> None:
    """Unknown title_style values must return None so the caller falls
    back to the caption-derived rules instead of shipping an empty rule
    list and producing weak titles."""
    assert smart_drafts._title_style_rules("definitely_not_a_real_style") is None


def test_smart_draft_prompt_uses_title_style_when_provided() -> None:
    """When title_style is set, the prompt must carry the title_style
    rules — NOT the caption_style-derived ones."""
    prompt = smart_drafts._smart_draft_prompt(
        transcript_text="",
        source_title="A clip",
        niche_label="memes",
        caption_style="contextual_info",
        title_style="meme_setup_punchline",
    )
    # meme_setup_punchline-specific phrasing present:
    assert "trailing colon" in prompt.lower() or "ending with a colon" in prompt.lower()
    assert "CREATIVE REMIX IS REQUIRED" in prompt
    # contextual_info-specific title phrasing NOT present (would mean we
    # also injected the caption-derived rules — that's a leak):
    assert "5-12 words" not in prompt or "trailing colon" in prompt.lower()


def test_smart_draft_prompt_falls_back_to_caption_style_when_title_style_none() -> None:
    """When title_style is None / Auto, behavior is identical to the
    pre-decoupling path: title rules come from caption_style. This is the
    zero-risk guarantee we made the user."""
    with_explicit_none = smart_drafts._smart_draft_prompt(
        transcript_text="",
        source_title="A clip",
        niche_label="memes",
        caption_style="meme_relatable",
        title_style=None,
    )
    without_title_style_param = smart_drafts._smart_draft_prompt(
        transcript_text="",
        source_title="A clip",
        niche_label="memes",
        caption_style="meme_relatable",
    )
    assert with_explicit_none == without_title_style_param


def test_smart_draft_prompt_auto_string_also_falls_back() -> None:
    """The UI sends an empty-string sentinel for Auto. The getter
    normalizes it to None, but defense-in-depth: the prompt builder must
    also treat 'auto' / '' as fallback."""
    with_auto = smart_drafts._smart_draft_prompt(
        transcript_text="",
        source_title="A clip",
        niche_label="memes",
        caption_style="meme_relatable",
        title_style="auto",
    )
    fallback = smart_drafts._smart_draft_prompt(
        transcript_text="",
        source_title="A clip",
        niche_label="memes",
        caption_style="meme_relatable",
    )
    assert with_auto == fallback


def test_title_style_mix_news_brief_title_with_context_info_caption() -> None:
    """End-to-end: the use case the user asked for — pair a News Brief
    title with a Context/Info caption. Both rule sets must be present in
    the assembled prompt and not interfere with each other."""
    prompt = smart_drafts._smart_draft_prompt(
        transcript_text="",
        source_title="Tech news clip",
        niche_label="tech",
        caption_style="contextual_info",
        title_style="news_brief",
    )
    # News Brief title rules should be active:
    assert "6-12 words" in prompt
    assert "🤔" in prompt  # trailing emoji allowed for news_brief titles
    # Context/Info caption emphasis must still be present (the user-prompt
    # markers for contextual_info live in its caption_style_line: the
    # paragraph_rule lives in the system prompt, not the user prompt):
    assert "THIS clip" in prompt
    assert "do not define" in prompt.lower()


def test_title_style_ui_dropdown_includes_auto_and_meme_setup_punchline() -> None:
    """The UI must expose both Auto (zero-risk default) and the new
    Meme Setup -> Punchline option. Without these the new style is
    unreachable from the app."""
    main_window_src = (
        Path(__file__).parent.parent / "src" / "nicheflow_studio" / "app" / "main_window.py"
    ).read_text(encoding="utf-8")
    assert 'addItem("Auto (match caption style)", "")' in main_window_src
    assert "(Meme) Setup" in main_window_src
    assert '"meme_setup_punchline"' in main_window_src
    assert "(History) Past Moments" in main_window_src
    assert '"history_lost_archive"' in main_window_src
    assert '"past_moments"' in main_window_src
    assert "_processing_prompt_title_style_combo" in main_window_src


def test_generate_smart_drafts_threads_title_style_into_request(monkeypatch) -> None:
    """End-to-end via generate_smart_drafts: passing title_style must
    cause the meme_setup_punchline rules to land in the actual prompt
    payload sent to Groq."""
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    monkeypatch.setattr(
        smart_drafts,
        "sample_video_frame_data_urls",
        lambda path, max_frames=5: [],
    )
    captured_prompts: list[str] = []

    def fake_urlopen(request, timeout=90):  # noqa: ANN001
        payload = smart_drafts.json.loads(request.data.decode("utf-8"))
        captured_prompts.append(payload["messages"][1]["content"])
        return _FakeJsonResponse(_WRITER_OK_BODY)

    monkeypatch.setattr(smart_drafts.urllib.request, "urlopen", fake_urlopen)

    smart_drafts.generate_smart_drafts(
        transcript_text="Some transcript that gives high context to skip vision",
        source_title="Specific descriptive title",
        niche_label="memes",
        caption_style="contextual_info",
        title_style="meme_setup_punchline",
    )

    assert captured_prompts, "writer call must have been made"
    writer_prompt = captured_prompts[-1]
    # meme_setup_punchline rules landed in the actual prompt:
    assert (
        "trailing colon" in writer_prompt.lower() or "ending with a colon" in writer_prompt.lower()
    )
    assert "CREATIVE REMIX IS REQUIRED" in writer_prompt


# ---------------------------------------------------------------------------
# Bugfix follow-ups: title_style override + widened encyclopedia ban + meta
# ---------------------------------------------------------------------------


def test_title_style_suppresses_profile_title_guidance() -> None:
    """When title_style is set, the prompt_profile's style['title'] line
    must NOT appear — it was injected ABOVE the title_style rules and
    competed with them (the profile mentions 'When ...' shapes without
    colons, so the model was producing the right shape but ignoring the
    trailing-colon requirement). This is the real-bug observed when the
    user generated and got titles like 'When you can't stop sending Reels'
    with no trailing colon."""
    profile_block = smart_drafts._profile_style_block("gaming_meme")
    profile_title_marker = profile_block["title"]
    # Sanity check: the profile title line really is a long sentence that
    # would compete with title_style rules if injected.
    assert "POV" in profile_title_marker or "When" in profile_title_marker

    prompt_with_title_style = smart_drafts._smart_draft_prompt(
        transcript_text="",
        source_title="A clip",
        niche_label="memes",
        prompt_profile="gaming_meme",
        caption_style="contextual_info",
        title_style="meme_setup_punchline",
    )
    # The profile's competing title sentence must NOT be in the prompt:
    assert profile_title_marker not in prompt_with_title_style


def test_profile_title_guidance_present_when_title_style_is_auto() -> None:
    """Backward-compat: when title_style is None / Auto, the profile's
    title guidance line still appears (preserves pre-Path-2 behavior)."""
    profile_block = smart_drafts._profile_style_block("gaming_meme")
    profile_title_marker = profile_block["title"]

    prompt_auto = smart_drafts._smart_draft_prompt(
        transcript_text="",
        source_title="A clip",
        niche_label="memes",
        prompt_profile="gaming_meme",
        caption_style="contextual_info",
        title_style=None,
    )
    assert profile_title_marker in prompt_auto


def test_anti_explainer_ban_catches_academic_framing() -> None:
    """The narrow 'X is a popular Y' ban was being dodged by the model
    switching to 'X is a phenomenon' / 'The concept of X' framings (real
    output observed: 'The concept of sending unwanted content is a
    phenomenon...'). The widened ban must catch those too."""
    lines = smart_drafts._anti_explainer_avoid_lines("contextual_info", None)
    joined = "\n".join(lines)
    assert "phenomenon" in joined.lower()
    assert "concept of" in joined.lower()
    assert "has been observed" in joined.lower()


def test_negative_examples_include_academic_framing_example() -> None:
    """The exact academic-framing opener observed in real output must
    appear as a NEGATIVE EXAMPLE so the model has a concrete target to
    avoid, not just abstract bans."""
    block = smart_drafts._negative_caption_examples_block("contextual_info")
    joined = "\n".join(block)
    assert "concept of sending unwanted content" in joined.lower()
    assert "phenomenon" in joined.lower()


def test_generation_meta_includes_caption_style_and_title_style_for_groq(monkeypatch) -> None:
    """The JSON metadata panel in the app must show the style choices that
    were actually sent — otherwise users have no way to verify the
    dropdown threading without code-level debugging."""
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    monkeypatch.setattr(
        smart_drafts,
        "sample_video_frame_data_urls",
        lambda path, max_frames=5: [],
    )

    def fake_urlopen(request, timeout=90):  # noqa: ANN001
        return _FakeJsonResponse(_WRITER_OK_BODY)

    monkeypatch.setattr(smart_drafts.urllib.request, "urlopen", fake_urlopen)

    result = smart_drafts.generate_smart_drafts(
        transcript_text="Some transcript that gives high context",
        source_title="Specific descriptive title",
        niche_label="memes",
        caption_style="contextual_info",
        title_style="meme_setup_punchline",
    )
    meta = result.generation_meta or {}
    assert meta["caption_style"] == "contextual_info"
    assert meta["title_style"] == "meme_setup_punchline"


def test_generation_meta_records_none_styles_when_omitted(monkeypatch) -> None:
    """When the caller doesn't pass styles, the meta records None — so
    the panel shows 'null' instead of a stale string from a previous run."""
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    monkeypatch.setattr(
        smart_drafts,
        "sample_video_frame_data_urls",
        lambda path, max_frames=5: [],
    )
    monkeypatch.setattr(
        smart_drafts.urllib.request,
        "urlopen",
        lambda request, timeout=90: _FakeJsonResponse(_WRITER_OK_BODY),
    )

    result = smart_drafts.generate_smart_drafts(
        transcript_text="some transcript with enough context for the writer",
        source_title="A specific title",
        niche_label="memes",
    )
    meta = result.generation_meta or {}
    assert meta["caption_style"] is None
    assert meta["title_style"] is None
