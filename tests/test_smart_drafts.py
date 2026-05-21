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
                b'\\"caption_options\\":[\\"This elephant stole the show\\",\\"You need to see this zoo moment\\",\\"The best part is the elephant reveal\\"]}"}}]}'
            )

    monkeypatch.setattr(smart_drafts.urllib.request, "urlopen", lambda request, timeout=90: FakeResponse())

    result = smart_drafts.generate_smart_drafts(
        transcript_text="Here is a funny clip about an elephant at the zoo.",
        source_title="Zoo clip",
        niche_label="animal comedy",
    )

    assert result.summary == "A funny zoo moment"
    assert result.title_options[0] == "Elephant Chaos"
    assert len(result.caption_options) == 3


def test_generate_smart_drafts_requires_provider(monkeypatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("OLLAMA_DISABLED", "1")

    with pytest.raises(RuntimeError, match="No smart-draft provider is configured"):
        smart_drafts.generate_smart_drafts(
            transcript_text="hello",
            source_title=None,
            niche_label=None,
        )


def test_generate_smart_drafts_prefers_ollama_when_enabled(monkeypatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_DISABLED", raising=False)
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:7b")
    monkeypatch.setattr(smart_drafts, "sample_video_frame_data_urls", lambda path, max_frames=3: [])
    captured_request = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
            return False

        def read(self) -> bytes:
            return (
                b'{"message":{"content":"{\\"summary\\":\\"A Minecraft farming moment\\",'
                b'\\"title_options\\":[\\"Minecraft Hoe Moment\\",\\"This Farming Clip Works\\",\\"Clean Hoe Timing\\"],'
                b'\\"caption_options\\":[\\"This Minecraft farming clip is all about how clean the hoe rhythm looks once the motion starts repeating. It feels simple, but the loop is what makes the whole moment satisfying to watch.\\",'
                b'\\"No dialogue here, just a very readable Minecraft farming sequence where the hoe movement carries the whole clip. The appeal is how immediate the payoff feels once you see the pattern lock in.\\",'
                b'\\"The clip works because it stays focused on one visible farming action and lets the repetition do the job. It is the kind of Minecraft moment that looks basic at first and then becomes weirdly satisfying as it keeps going.\\"]}"}}'
            )

    def fake_urlopen(request, timeout=90):  # noqa: ANN001
        captured_request["url"] = request.full_url
        captured_request["payload"] = smart_drafts.json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(smart_drafts.urllib.request, "urlopen", fake_urlopen)

    result = smart_drafts.generate_smart_drafts(
        transcript_text="",
        source_title="Hoe hoe hoe 2",
        niche_label="minecraft gameplay",
    )

    assert result.provider_label == "Ollama Qwen 2.5 7B"
    assert captured_request["url"] == "http://127.0.0.1:11434/api/chat"
    assert captured_request["payload"]["model"] == "qwen2.5:7b"
    assert captured_request["payload"]["format"] == "json"
    assert result.title_options[0] == "Minecraft Hoe Moment"


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

    monkeypatch.setattr(smart_drafts.urllib.request, "urlopen", lambda request, timeout=90: FakeResponse())

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
    assert "at least 70 words" in payload["messages"][0]["content"]
    assert "reasoning_effort" not in payload
    assert "response_format" not in payload


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


def test_generate_smart_drafts_ignores_missing_vision_summary_when_groq_vision_is_forbidden(monkeypatch) -> None:
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
        if request.full_url == smart_drafts.GROQ_CHAT_COMPLETIONS_URL and payload["model"] == "meta-llama/llama-4-scout-17b-16e-instruct":
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
    assert "\"scene_summary\": \"(none)\"" in captured_payloads[1]["messages"][1]["content"]
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
    assert "Do not return captions under 70 words" in prompt
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

    assert "Original source caption (high-priority context)" in prompt
    assert "Passenger is a horror trailer" in prompt
    assert "Do not copy it directly" in prompt
    assert "Caption emphasis: lead with the relatable moment or feeling first" in prompt
    assert "one sentence max" in prompt
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

    assert "Visual-first mode: no transcript is available." in prompt
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


def test_clean_options_collapses_newlines_for_titles() -> None:
    cleaned = smart_drafts._clean_options(["Line one\nLine two"])

    assert cleaned == ["Line one Line two"]


def test_normalize_caption_text_collapses_excess_blank_lines() -> None:
    normalized = smart_drafts._normalize_caption_text(
        "Para one.\n\n\n\nPara two.   \n\n#tag"
    )

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
                        {"type": "output_text", "text": "{\"summary\":\"ok\",\"title_options\":[\"A\",\"B\",\"C\"],\"caption_options\":[\"one\",\"two\",\"three\"]}"},
                    ]
                }
            }
        ]
    }

    content = smart_drafts._extract_message_content(payload)

    assert "First line" in content
    assert "\"summary\":\"ok\"" in content


def test_extract_message_content_handles_nested_content_parts() -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": [
                        [
                            {"type": "output_text", "text": "{\"scene_summary\":\"A Minecraft path\"}"}
                        ]
                    ]
                }
            }
        ]
    }

    content = smart_drafts._extract_message_content(payload)

    assert "\"scene_summary\":\"A Minecraft path\"" in content


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

    monkeypatch.setattr(smart_drafts.urllib.request, "urlopen", lambda request, timeout=90: FakeResponse())

    result = smart_drafts.generate_smart_drafts(
        transcript_text="A silent Minecraft farming clip with a hoe.",
        source_title="Hoe hoe hoe 2",
        niche_label="minecraft gameplay",
    )

    assert result.provider_label == "Local fallback"
    assert result.used_fallback is True
