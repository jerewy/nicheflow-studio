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


def test_duration_claim_accepts_spelled_out_number_in_signals() -> None:
    # Item #2108: the source wrote "Sixty-nine years later" and the guard demoted
    # "Same balcony, 69 years apart" because it only looked for the digits.
    signals = "Her first appearance was in 1953. Sixty-nine years later, she returned."

    supported = draft_guard.check_title_claims("Same balcony, 69 years apart", signals)
    unsupported = draft_guard.check_title_claims("Same balcony, 68 years apart", signals)

    assert supported.unsupported_terms == []
    assert unsupported.unsupported_terms == ["68 year"]


def test_year_span_claim_accepts_two_signal_years_that_far_apart() -> None:
    signals = "First balcony appearance in June 1953, last one in June 2022."

    supported = draft_guard.check_title_claims("Same balcony, 69 years apart", signals)
    unsupported = draft_guard.check_title_claims("Same balcony, 70 years apart", signals)

    assert supported.unsupported_terms == []
    assert unsupported.unsupported_terms == ["70 year"]


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


def test_group_claim_from_the_contact_sheet_does_not_clear_green() -> None:
    """A setting invented off the frames used to be the one unguarded category.

    Real case: "What made a room full of psychiatrists laugh at that question?"
    over footage of two people interviewed SEPARATELY (one at a convention, one
    on a city street). It cleared green because "room" was in no lexicon, while
    the accurate option naming the convention came back yellow - the guard was
    rewarding the vaguer, wrong title.
    """
    signals = draft_guard.build_signals_text(
        "A female psychiatrist at the 2006 APA Convention breaks into nervous "
        "laughter when asked how many patients she has cured. The clip then cuts "
        "to a gray haired doctor on a city street confessing the number is zero.",
        None,
        "history",
        None,
    )
    report = draft_guard.check_title_claims(
        "What made a room full of psychiatrists laugh at that question?", signals
    )
    assert "room full of" in report.unsupported_terms


def test_group_claim_backed_by_the_signals_is_supported() -> None:
    signals = draft_guard.build_signals_text(
        "Footage from inside the classroom shows a room full of children reading "
        "aloud while the president sits with them.",
        None,
        "history",
        None,
    )
    report = draft_guard.check_title_claims(
        "A room full of children kept reading aloud.", signals
    )
    assert report.unsupported_terms == []


def test_visible_but_undescribed_settings_are_not_group_claims() -> None:
    """"on stage" and a bare "audience" were deliberately left out of the lexicon.

    A stage is usually visible rather than written down, so listing it flagged 17
    of 23 legitimate performance titles. Only claims that several people were
    together in one place are tracked.
    """
    signals = draft_guard.build_signals_text(
        "Michael Jackson performing during the 1995 tour.", None, "history", None
    )
    report = draft_guard.check_title_claims("Michael Jackson lost it on stage.", signals)
    assert report.unsupported_terms == []


# The item-1959 case: the source caption described Iran's Qajar dynasty (ended
# 1925) while the footage showed 1970s street scenes, so the drafting model
# wrote its own mismatch finding into all three title slots. Every option
# cleared green (nothing checkable was asserted) and Apply burned one onto the
# video. A title is viewer-facing; metadata is not.
QAJAR_SIGNALS = (
    "Iran's royal family lived a secluded life in the Qajar dynasty. "
    "The Qajar dynasty reigned Iran from 1789 to 1925."
)
META_TITLE = (
    "The caption attached to this clip is about a dynasty gone a century ago. "
    "What's on screen is not."
)


def test_metadata_referential_title_is_detected() -> None:
    assert draft_guard.find_meta_references(META_TITLE)


def test_ordinary_titles_are_not_metadata_references() -> None:
    for title in (
        "The flag these water skiers are carrying has been banned in Iran since 1979.",
        "This clip was banned for 40 years.",
        "The source of the Nile stayed a mystery for centuries.",
        "Then the caption under it read a name everyone recognized.",
        "Snow, pine trees, and nothing in the footage reads as a chase.",
    ):
        assert draft_guard.find_meta_references(title) == [], title


def test_metadata_title_goes_red_even_though_it_claims_nothing() -> None:
    # Without the meta check this title is green: it asserts no checkable fact.
    assert draft_guard.check_title_claims(META_TITLE, QAJAR_SIGNALS).claim_terms == []

    guarded = draft_guard.guard_options(
        title_options=[META_TITLE],
        signals_text=QAJAR_SIGNALS,
    )

    assert guarded.option_tiers == ["red"]
    assert 0 in guarded.meta_terms
    assert "Not a viewer-facing title" in (guarded.option_notes or [""])[0]


def test_recommendation_moves_off_a_metadata_title() -> None:
    guarded = draft_guard.guard_options(
        # The alternative must be claim-free too, or it reddens on its own
        # (a bare "1970s" is a date claim these signals do not support).
        title_options=[META_TITLE, "An ordinary afternoon that feels impossible now."],
        signals_text=QAJAR_SIGNALS,
        recommended_index=0,
        recommendation_reason="Stays true to the mismatch.",
    )

    assert guarded.recommendation_shifted is True
    assert guarded.recommended_index == 1
    assert "metadata" in (guarded.recommendation_reason or "")


def test_adverbial_first_is_not_a_precedence_claim() -> None:
    # "first" meaning "before that" asserts no precedence, but the signals
    # almost never carry the literal word, so it used to come back unsupported
    # and drag the recommendation off a perfectly good option. The Chaplin case:
    # a verified twelve-minute ovation lost the pick to an option naming nobody.
    signals = "Charlie Chaplin accepts an honorary award as the audience applauds."

    adverbial = draft_guard.check_title_claims(
        "The Academy gave him a lifetime achievement award, and the audience "
        "gave him twelve straight minutes of applause first.",
        signals,
    )

    assert adverbial.claim_terms == []
    assert adverbial.unsupported_terms == []


def test_precedence_first_is_still_checked() -> None:
    signals = "Charlie Chaplin accepts an honorary award as the audience applauds."

    unsupported = draft_guard.check_title_claims(
        "Chaplin's first return to America in twenty years", signals
    )
    supported = draft_guard.check_title_claims(
        "Chaplin's first return to America in twenty years",
        "Chaplin makes his first trip back to America in twenty years, since 1952.",
    )

    # "twenty years" is a duration claim as well, checked since spelled-out
    # durations in titles stopped slipping past the guard.
    assert unsupported.unsupported_terms == ["first", "20 year"]
    assert supported.unsupported_terms == []


def test_short_limit_reddens_overlong_option_and_moves_the_pick() -> None:
    long_title = (
        "She walked into the room, everyone went quiet, and then the crowd slowly "
        "realised who had just arrived at the party"
    )
    guarded = draft_guard.guard_options(
        title_options=["Nobody expected her at the party", long_title],
        signals_text=PRAM_SIGNALS,
        recommended_index=1,
        recommendation_reason="most beats",
        max_title_chars=draft_guard.SHORT_TITLE_MAX_CHARS,
    )

    assert guarded.option_tiers[1] == "red"
    assert "Too long for this account's Short title setting" in guarded.option_notes[1]
    assert guarded.recommendation_shifted is True
    assert guarded.recommended_index == 0
    assert "over the Short title limit" in guarded.recommendation_reason


def test_no_limit_leaves_long_titles_alone() -> None:
    long_title = "x " * 80
    guarded = draft_guard.guard_options(
        title_options=[long_title.strip()],
        signals_text=PRAM_SIGNALS,
        recommended_index=0,
    )

    assert guarded.option_tiers == ["green"]
    assert guarded.recommendation_shifted is False


def test_only_the_short_setting_has_a_character_limit() -> None:
    assert draft_guard.title_char_limit("short") == draft_guard.SHORT_TITLE_MAX_CHARS
    assert draft_guard.title_char_limit(" Short ") == draft_guard.SHORT_TITLE_MAX_CHARS
    for other in ("auto", "long", "medium", None, ""):
        assert draft_guard.title_char_limit(other) is None


def test_spelled_out_duration_in_the_title_is_checked_too() -> None:
    # Blind-test reel #2381 shipped green: "Six months after this photo, the man
    # beside Tupac was accused..." The guard only looked for digit durations in
    # the TITLE, so a spelled-out "six months" was never checked at all.
    signals = (
        "The last photo ever taken of rapper Tupac Shakur, just 20 minutes before "
        "his death in a drive-by shooting. 1996."
    )

    unsupported = draft_guard.check_title_claims(
        "Six months after this photo, the man beside Tupac was accused of a murder",
        signals,
    )
    supported = draft_guard.check_title_claims(
        "Twenty minutes after this photo, Tupac was shot", signals
    )
    idiom = draft_guard.check_title_claims("One day in 1996, Tupac posed for this photo", signals)

    assert unsupported.unsupported_terms == ["6 month"]
    assert supported.unsupported_terms == []
    # "one day" is an idiom far more often than a timeline claim.
    assert idiom.claim_terms == ["1996"]
