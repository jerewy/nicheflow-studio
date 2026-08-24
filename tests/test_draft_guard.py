"""Unit tests for the deterministic grounding guard (the "heavy pram" fix).

The guard's contract: dramatic/interpretive titles pass untouched, concrete
claims need evidence in the clip signals (with paraphrase-aware stems), an
unsupported claim downgrades the option to red, and a red option can never
stay the recommended pick when a clean alternative exists.
"""

from __future__ import annotations

from nicheflow_studio.processing import draft_guard

# Signals with NO weight evidence: "heavy" must get flagged against these.
PRAM_SIGNALS = (
    "Vintage prams on the streets of London, 1950s. "
    "Black vintage baby carriages with large wheels."
)


def test_green_title_makes_no_claims() -> None:
    report = draft_guard.check_title_claims(
        "Would you push one of these today?", PRAM_SIGNALS
    )

    assert report.claim_terms == []
    assert report.unsupported_terms == []


def test_unsupported_heavy_is_flagged() -> None:
    report = draft_guard.check_title_claims(
        "These prams were insanely heavy", PRAM_SIGNALS
    )

    assert report.unsupported_terms == ["heavy"]


def test_paraphrased_weight_counts_as_support() -> None:
    signals = PRAM_SIGNALS + " Each pram weighed more than a modern bicycle."

    report = draft_guard.check_title_claims("These prams were insanely heavy", signals)

    assert report.claim_terms == ["heavy"]
    assert report.unsupported_terms == []


def test_numeric_unit_evidence_counts_as_support() -> None:
    signals = PRAM_SIGNALS + " Cast iron frame, about 30kg of steel."

    report = draft_guard.check_title_claims("These prams were insanely heavy", signals)

    assert report.unsupported_terms == []


def test_year_claim_needs_the_year_in_signals() -> None:
    supported = draft_guard.check_title_claims("Pram fashion in the 1950s", PRAM_SIGNALS)
    unsupported = draft_guard.check_title_claims(
        "Pram fashion in 1903", PRAM_SIGNALS
    )

    assert supported.unsupported_terms == []
    assert unsupported.unsupported_terms == ["1903"]


def test_finality_claim_needs_signal_support() -> None:
    # "final"/"never again"/"only time" read as verified biography facts; they
    # pass only when the source itself says so (the MJ "last rehearsal" case —
    # the famous 36-hours clip was NOT his last rehearsal).
    signals = "Michael Jackson rehearsing This Is It on stage with dancers."

    unsupported = draft_guard.check_title_claims(
        "His final rehearsal before the comeback", signals
    )
    supported = draft_guard.check_title_claims(
        "His final rehearsal before the comeback",
        signals + " The caption called it his last rehearsal.",
    )
    never_again = draft_guard.check_title_claims(
        "He would never again step on a stage", signals
    )
    only_time = draft_guard.check_title_claims(
        "The only time this was ever filmed", signals
    )

    assert unsupported.unsupported_terms == ["final"]
    assert supported.unsupported_terms == []
    assert never_again.unsupported_terms == ["never again"]
    assert only_time.unsupported_terms == ["only time"]


def test_duration_claim_needs_the_same_number_in_signals() -> None:
    # Regression for item #221: "36 hours before his death" is fine when the
    # overlay says 36, but a silently changed number must be flagged.
    signals = (
        "On-screen text: This was 36 hours before Michael Jackson's death. "
        "Rehearsal footage on a production stage."
    )

    supported = draft_guard.check_title_claims(
        "Michael Jackson rehearsed his comeback just 36 hours before his death",
        signals,
    )
    unsupported = draft_guard.check_title_claims(
        "Michael Jackson rehearsed his comeback just 14 hours before his death",
        signals,
    )

    assert supported.unsupported_terms == []
    assert unsupported.unsupported_terms == ["14 hour"]


def test_verified_citation_clears_a_paraphrased_claim() -> None:
    signals = PRAM_SIGNALS + " Only three of these are known to survive today."

    without_citation = draft_guard.check_title_claims(
        "One of the rarest prams ever made", signals
    )
    with_citation = draft_guard.check_title_claims(
        "One of the rarest prams ever made",
        signals,
        citation="Only three of these are known to survive",
    )

    assert without_citation.unsupported_terms == ["rarest"]
    assert with_citation.unsupported_terms == []


def test_invented_citation_does_not_clear_the_claim() -> None:
    report = draft_guard.check_title_claims(
        "These prams were insanely heavy",
        PRAM_SIGNALS,
        citation="weighed over 30 kilograms",  # not actually in the signals
    )

    assert report.unsupported_terms == ["heavy"]


def test_guard_derives_tiers_for_untier_paste_drafts() -> None:
    guarded = draft_guard.guard_options(
        title_options=[
            "Would you push one of these today?",
            "Pram fashion in the 1950s",
            "These prams were insanely heavy",
        ],
        signals_text=PRAM_SIGNALS,
    )

    assert guarded.option_tiers == ["green", "yellow", "red"]
    assert guarded.flagged_terms == {2: ["heavy"]}
    assert "heavy" in guarded.option_notes[2]


def test_guard_downgrades_model_tier_but_keeps_clean_ones() -> None:
    guarded = draft_guard.guard_options(
        title_options=[
            "Would you push one of these today?",
            "These prams were insanely heavy",
        ],
        signals_text=PRAM_SIGNALS,
        option_tiers=["green", "green"],  # the model under-rated its own claim
        option_notes=["clearest hook", "boldest angle"],
    )

    assert guarded.option_tiers == ["green", "red"]
    assert guarded.option_notes[0] == "clearest hook"
    assert guarded.option_notes[1].startswith("boldest angle [Grounding check:")


def test_recommendation_shifts_to_best_clean_option() -> None:
    guarded = draft_guard.guard_options(
        title_options=[
            "Would you push one of these today?",
            "Pram fashion in the 1950s",
            "These prams were insanely heavy",
        ],
        signals_text=PRAM_SIGNALS,
        recommended_index=2,
        recommendation_reason="boldest hook",
    )

    assert guarded.recommendation_shifted is True
    assert guarded.recommended_index == 0  # green beats yellow
    assert guarded.recommendation_reason == (
        "boldest hook [Auto-moved from Option 3: 'heavy' has no support in the clip signals.]"
    )


def test_recommendation_kept_with_warning_when_no_clean_alternative() -> None:
    guarded = draft_guard.guard_options(
        title_options=["These prams were insanely heavy", "The rarest pram in history"],
        signals_text=PRAM_SIGNALS,
        recommended_index=0,
        recommendation_reason="boldest hook",
    )

    assert guarded.recommendation_shifted is False
    assert guarded.recommended_index == 0
    assert "verify before posting" in guarded.recommendation_reason


def test_model_red_tier_blocks_recommendation_too() -> None:
    # The prompt says red means "you should not have written it"; if the model
    # tags red AND recommends it anyway, the guard still moves the pick.
    guarded = draft_guard.guard_options(
        title_options=["A calm look at old London", "Footage they tried to hide"],
        signals_text=PRAM_SIGNALS,
        option_tiers=["green", "red"],
        recommended_index=1,
    )

    assert guarded.recommendation_shifted is True
    assert guarded.recommended_index == 0
    assert "unverifiable claim" in guarded.recommendation_reason


def test_build_signals_text_skips_empty_and_serializes_dicts() -> None:
    text = draft_guard.build_signals_text(
        "A transcript.",
        None,
        "(none)",
        {"on_screen_hook": "weighed 30kg"},
    )

    assert "A transcript." in text
    assert "weighed 30kg" in text
    assert "(none)" not in text


# --- Source-caption provenance ---------------------------------------------
# The scraped post's caption is another repost account's claim, not evidence we
# observed. It identifies the subject but must never clear a checkable fact,
# otherwise their embellished dates and records become our confident titles.


def test_claim_backed_only_by_source_caption_is_asserted_not_supported() -> None:
    report = draft_guard.check_title_claims(
        "The heaviest pram ever sold in Britain",
        PRAM_SIGNALS,
        asserted_text="Fun fact: this was the heaviest pram ever made!",
    )

    assert report.asserted_terms == ["heaviest"]
    assert report.unsupported_terms == []


def test_claim_in_real_signals_still_clears_even_with_asserted_text() -> None:
    report = draft_guard.check_title_claims(
        "The heaviest pram on the street",
        PRAM_SIGNALS + " One weighed 30kg.",
        asserted_text="the heaviest pram ever made",
    )

    assert report.asserted_terms == []
    assert report.unsupported_terms == []


def test_claim_in_neither_source_stays_red() -> None:
    report = draft_guard.check_title_claims(
        "The heaviest pram ever sold",
        PRAM_SIGNALS,
        asserted_text="Lovely old photo from London.",
    )

    assert report.unsupported_terms == ["heaviest"]
    assert report.asserted_terms == []


def test_citation_quoting_the_source_caption_flags_instead_of_clearing() -> None:
    report = draft_guard.check_title_claims(
        "The heaviest pram ever sold",
        PRAM_SIGNALS,
        citation="the heaviest pram ever made",
        asserted_text="Fun fact: the heaviest pram ever made!",
    )

    assert report.asserted_terms == ["heaviest"]
    assert report.unsupported_terms == []


def test_guard_marks_source_only_claim_yellow_with_a_review_note() -> None:
    guarded = draft_guard.guard_options(
        title_options=["The heaviest pram ever sold in Britain"],
        signals_text=PRAM_SIGNALS,
        asserted_signals_text="Fun fact: this was the heaviest pram ever made!",
        option_tiers=["green"],
        recommended_index=0,
    )

    assert guarded.option_tiers == ["yellow"]
    assert guarded.recommended_index == 0  # usable, just flagged
    assert guarded.option_notes is not None
    assert "only from the original poster's caption" in guarded.option_notes[0]


def test_guard_without_asserted_text_behaves_exactly_as_before() -> None:
    guarded = draft_guard.guard_options(
        title_options=["The heaviest pram ever sold"],
        signals_text=PRAM_SIGNALS,
        recommended_index=0,
    )

    assert guarded.option_tiers == ["red"]
    assert guarded.flagged_terms == {0: ["heaviest"]}


# --- Venue claims -----------------------------------------------------------
# Real failure: a title said "On the palace lawn" over footage shot on the
# Government House lawn in Auckland, and the top comment was the correction.


def test_invented_venue_is_flagged() -> None:
    report = draft_guard.check_title_claims(
        "On the palace lawn, Diana lifted baby William toward Charles.",
        "Royal photocalls staged relaxed on-the-grass sessions with young "
        "Prince William for the press. Diana and Charles sit on a blanket.",
    )

    assert report.unsupported_terms == ["palace"]


def test_venue_named_in_the_signals_passes() -> None:
    report = draft_guard.check_title_claims(
        "On the palace lawn, Diana lifted baby William toward Charles.",
        "Filmed on the Buckingham Palace lawn during a 1983 photocall.",
    )

    assert report.unsupported_terms == []


def test_venue_only_in_the_reposter_caption_is_flagged_for_review() -> None:
    report = draft_guard.check_title_claims(
        "The moment inside the cathedral",
        "A choir performs in a large stone hall.",
        asserted_text="Recorded inside the cathedral in 1974.",
    )

    assert report.asserted_terms == ["cathedral"]
    assert report.unsupported_terms == []
