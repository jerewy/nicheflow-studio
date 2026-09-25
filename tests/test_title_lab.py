"""Tests for the experimental title bench.

``title_lab`` is not on the shipping draft path, so these cover the two things
that would silently corrupt a comparison rather than the prompt text itself: the
batch parser's clip attribution, and the length check's coupling to the real
renderer.
"""

from __future__ import annotations

from nicheflow_studio.processing import title_lab

NL = chr(10)


def test_parse_titles_drops_preamble_and_trailing_sections():
    reply = """Here are the six options:

1. This dog could not decide who to follow
- Turns out the swinging was never really about the webs
"The Sphere has opinions about these landings"

Recommended Pick: Title Option 2
Why: it names the subject.
"""
    assert title_lab.parse_titles(reply) == [
        "This dog could not decide who to follow",
        "Turns out the swinging was never really about the webs",
        "The Sphere has opinions about these landings",
    ]


def test_parse_batch_titles_keeps_clips_separate():
    reply = """===== CLIP 1 =====
This dog could not decide who to follow
He picked neither and just stood there
===== CLIP 2 =====
Young Destiny's Child singing on the front steps
"""
    assert title_lab.parse_batch_titles(reply) == {
        1: [
            "This dog could not decide who to follow",
            "He picked neither and just stood there",
        ],
        2: ["Young Destiny's Child singing on the front steps"],
    }


def test_parse_batch_titles_returns_empty_for_a_single_clip_reply():
    # How ``score`` tells a batch reply from a single-clip one. If this ever
    # returned a truthy block for an undelimited reply, every single-clip run
    # would be scored as one anonymous clip.
    assert title_lab.parse_batch_titles("just one line here for one clip") == {}


def test_check_length_passes_a_title_that_fits_the_overlay():
    assert (
        title_lab.check_length(
            "This dog stopped every few steps trying to decide which way to go"
        )
        is None
    )


def test_check_length_flags_a_title_too_short_to_perform():
    reason = title_lab.check_length("That truck again")
    assert reason is not None
    assert "weakest measured" in reason


def test_check_length_flags_a_title_the_renderer_cannot_fit():
    # The renderer does not truncate: past four lines at the font floor it dumps
    # every remaining word onto the last line, which runs off the frame. This
    # test is also what pins _OVERLAY_FONT_FLOOR / _OVERLAY_BASE_FONT to the
    # constants inside video._fit_title_overlay - if those drift, the budget
    # computed here drifts with them and this stops failing the way it should.
    paragraph = (
        "In 1998 Warner Bros spent around ten million dollars on test footage for a "
        "photorealistic remake of The Incredible Mr Limpet starring Jim Carrey, and the "
        "early computer graphics showed a fish with Carrey's face attached to it, "
        "blinking and moving its mouth underwater, which test audiences found so "
        "unsettling that the studio quietly cancelled the entire project"
    )
    reason = title_lab.check_length(paragraph)
    assert reason is not None
    assert "overflows" in reason
    assert "off-frame" in reason


def test_check_length_accepts_the_top_of_the_usable_window():
    # ~24 words is where the overlay stops fitting; this one must still pass so
    # the check never squeezes the range tighter than the renderer does.
    assert (
        title_lab.check_length(
            "An Illinois couple turned their entire property into a forest and from "
            "satellite view it now looks like one solid green square"
        )
        is None
    )


def test_single_clip_and_batch_prompts_carry_the_same_rules():
    # The two builders each used to compute the variant's rule flags themselves,
    # and a flag added to one silently skipped the other - which would make a
    # batch run and a single-clip run of "the same" variant incomparable.
    evidence = title_lab.ClipEvidence(account_name="a", scraped_caption="b")
    for variant in (
        title_lab.VARIANT_V2_OPEN_I,
        title_lab.VARIANT_V2_NO_I,
        title_lab.VARIANT_V2_NAMED,
    ):
        single = title_lab.build_prompt(evidence, variant=variant, seed=1)
        batch = title_lab.build_batch_prompt([evidence], variant=variant, seed=1)
        for rule in title_lab._rules_for(variant):
            assert rule in single, f"{variant}: single-clip prompt missing a rule"
            assert rule in batch, f"{variant}: batch prompt missing a rule"


def test_the_person_rule_permits_viewer_reactions_and_blocks_the_real_failure():
    """The ban is scoped to what actually went wrong, not to the pronoun.

    A flat "never use I" contradicted the example block: this account's #2 and
    #3 titles by measured reach both open in first person, and they were being
    shown as winners directly under a rule forbidding them. The line that failed
    review was never a viewer reaction - it was first person about a person on
    screen ("I would let her tell me a bedtime story any day" over a swimsuit
    clip), which is what the rule names now.
    """
    rules = "\n".join(title_lab._v2_rules(allow_singular_i=False))
    assert "Do not use I, me, my or mine" not in rules
    assert "I had never heard that" in rules          # the permitted register
    assert "bedtime story any day" in rules            # the named failure
    assert "never to who you find attractive" in rules


def test_the_accounts_own_winners_survive_the_person_rule():
    # Regression: filtering first-person examples to satisfy a flat ban would
    # have dropped this account's two best-measured titles from the teaching set.
    winners = [
        "I had never heard that Michael Jackson came backstage to meet the cast",
        "Janet Jackson turned grief into a VMA tribute",
    ]
    rules = "\n".join(title_lab.production_title_rules(few_shot_winners=winners, seed=1))
    for winner in winners:
        assert winner in rules


def test_no_variant_currently_permits_an_emoji():
    # Turned off after seeing the burn-in: the export rasterises with Segoe UI
    # Emoji, so the artwork frozen into the frame is the Windows drawing rather
    # than the phone one. Every variant must ban them until a font is bundled.
    evidence = title_lab.ClipEvidence(account_name="a", scraped_caption="b")
    for variant in title_lab.VARIANTS:
        prompt = title_lab.build_prompt(evidence, variant=variant, seed=1)
        assert "ONE EMOJI IS ALLOWED" not in prompt, variant
    for variant in (title_lab.VARIANT_V2_OPEN_I, title_lab.VARIANT_V2_NO_I, title_lab.VARIANT_V2_NAMED):
        assert "double hyphen, emoji, hashtag" in title_lab.build_prompt(
            evidence, variant=variant, seed=1
        ), variant


def test_the_emoji_rule_still_works_when_switched_on():
    # The capability is kept, just unused, so this covers it directly rather
    # than through a variant - otherwise turning it back on later ships untested.
    with_emoji = title_lab._v2_rules(allow_singular_i=False, allow_emoji=True)
    joined = "\n".join(with_emoji)
    assert "ONE EMOJI IS ALLOWED" in joined
    assert "double hyphen, hashtag" in joined  # emoji dropped from the ban list
    without = "\n".join(title_lab._v2_rules(allow_singular_i=False))
    assert "ONE EMOJI IS ALLOWED" not in without
    assert "double hyphen, emoji, hashtag" in without


def test_production_prompt_asks_for_three_titles_under_the_importable_headers():
    evidence = title_lab.ClipEvidence(account_name="a", scraped_caption="b")
    delimiters = ["===== REEL 1 (#524) =====", "===== REEL 2 (#523) ====="]
    prompt = title_lab.build_production_batch_prompt(
        [evidence, evidence],
        variant=title_lab.VARIANT_V2_NAMED,
        delimiters=delimiters,
        caption_rules=["- caption rule"],
        seed=1,
    )
    for delimiter in delimiters:
        assert delimiter in prompt
    assert "Title Option 1:" in prompt
    assert "Title Option 3:" in prompt
    assert "Title Option 4:" not in prompt
    assert "DIFFERENT BEATS" in prompt  # or the three come back as one hook reworded
    assert "Shared Caption:" in prompt
    assert "must be PLAIN TEXT" in prompt


def test_production_prompt_refuses_a_delimiter_count_mismatch():
    # Silent misalignment here would put reel 2's title on reel 3's clip.
    evidence = title_lab.ClipEvidence(account_name="a", scraped_caption="b")
    try:
        title_lab.build_production_batch_prompt(
            [evidence, evidence],
            variant=title_lab.VARIANT_V2_NAMED,
            delimiters=["===== REEL 1 (#1) ====="],
            caption_rules=["- caption rule"],
            seed=1,
        )
    except ValueError as exc:
        assert "2 clips but 1 delimiters" in str(exc)
    else:
        raise AssertionError("expected a ValueError")


def test_production_reply_round_trips_through_the_shipping_parser():
    # The whole point of this format: the app's own parser must read it back.
    from nicheflow_studio.services import draft_handoff

    reply = (
        "===== REEL 1 (#524) =====\nTitle Option 1:\nThe truck was doing the swinging all along\n"
        "Shared Caption:\nHow the swing shots were really made.\n\n"
        "===== REEL 2 (#523) =====\nTitle Option 1:\nA fan lifted the ring mid handshake\n"
        "Shared Caption:\nThe handshake that cost a ring.\n"
    )
    blocks = sorted(draft_handoff._split_batch_blocks(reply), key=lambda b: b[0])
    assert [reel for reel, _, _ in blocks] == [1, 2]
    assert [echoed for _, echoed, _ in blocks] == [524, 523]
    for _, _, body in blocks:
        parsed = draft_handoff.parse_pasted_draft(body)
        assert len(parsed.title_options) == 1
        assert parsed.title_options[0]
        # import_pasted_draft rejects any option carrying a title and no caption,
        # which a titles-only reply does for every reel. Asking for the shared
        # caption is what keeps this format importable, so pin it here: the
        # parser alone passing is not evidence that the import will.
        paired = list(zip(parsed.title_options, parsed.caption_options))
        assert not [i for i, (t, c) in enumerate(paired) if t.strip() and not c.strip()]


def test_a_titles_only_reply_would_be_rejected_on_import():
    # Regression guard for the mistake this format exists to avoid: a reply with
    # no caption parses perfectly and then fails import_pasted_draft's pairing
    # check on every reel. Passing the parser is not evidence the import works.
    from nicheflow_studio.services import draft_handoff

    parsed = draft_handoff.parse_pasted_draft(
        "===== REEL 1 (#524) =====\nTitle Option 1:\nA title carrying no caption\n"
    )
    assert parsed.title_options == ["A title carrying no caption"]
    paired = list(zip(parsed.title_options, parsed.caption_options))
    rejected = [i + 1 for i, (t, c) in enumerate(paired) if t.strip() and not c.strip()]
    assert rejected == [1]


def test_the_prompt_states_a_ceiling_the_renderer_actually_enforces():
    # The prompt's stated ceiling and check_length must agree. If the prompt says
    # 135 but the renderer breaks at 120, every batch ships titles that overflow
    # while technically obeying the rule.
    evidence = title_lab.ClipEvidence(account_name="a", scraped_caption="b")
    prompt = title_lab.build_prompt(evidence, variant=title_lab.VARIANT_V2_NAMED, seed=1)
    assert "130 CHARACTERS" in prompt

    at_ceiling = "The truck was doing the swinging the whole time and nobody watching the film ever noticed it happening"
    assert len(at_ceiling) <= 130
    assert title_lab.check_length(at_ceiling) is None, "a title at the stated ceiling must render"


def test_a_contradicted_premise_becomes_every_option_s_last_beat():
    """The debunk is a shape, not a reason to drop the clip.

    A real batch researched the Neymar ring theft, found he had denied it and
    was not wearing a ring, put the correction in ONE option and then wrote two
    more asserting the theft as fact - the different-beats rule asked for three
    angles and two of the available angles were the false version. Either of
    those, if picked, ships the false claim. The rule turns the correction into
    the last beat of every option instead, which is also the highest-performing
    shape available: the viral version is the setup, the truth is the turn.
    """
    rules = "\n".join(title_lab.production_title_rules(seed=1))
    assert "CORRECTION IS THE LAST BEAT OF EVERY OPTION" in rules
    assert "never let one option carry the correction while the others" in rules
    # Half-read protection: a skimmed title must not assert the false version.
    assert "the turn must be the LAST thing the line says" in rules


def test_pick_rule_treats_support_as_a_gate_not_the_reason():
    # 2026-09-14 batch: picks were justified by "supported, without overstating"
    # and landed on narration and an unparseable sentence over verdict options.
    rules = "\n".join(title_lab.production_title_rules(seed=1))
    assert "RECOMMENDED PICK, IN THIS ORDER" in rules
    assert "This is a gate, not the reason to pick" in rules
    assert "does not read cleanly in one breath" in rules
    assert "not that it is supported" in rules
    for phrase in ("says it all", "went viral", "more than words ever could"):
        assert phrase in rules


def test_the_hook_exception_hands_over_no_copyable_sentence():
    """Illustrative phrasing in a rule becomes output.

    The first version of the source-hook exception contained a writable line
    ("a week in and he is already driving like this") and the next batch
    returned it near-verbatim, exactly as the old prompt's "It looked like"
    opened 50 of 360 generated titles. A rule may describe the move; it may not
    hand over a sentence that fits the clip in front of it.
    """
    rules = "\n".join(title_lab.production_title_rules(seed=1))
    assert "THE SOURCE'S OWN ON-SCREEN HOOK IS NOT EVIDENCE" in rules
    assert "driving like this" not in rules
    # The rule must not license stating an unsupported hook claim as fact.
    assert "Carry it the way the source does" not in rules
    assert "do not state it as fact" in rules


def test_the_ending_rule_leads_on_the_verdict_not_the_beat_count():
    """What the loudest rule teaches is what the batch comes back as.

    The old bullet ran 1,771 characters and opened by calling beats "the single
    biggest thing separating the winners from the rest". Eight reels in one batch
    then came back narrating ("He walked a few steps, stopped, turned back toward
    her laugh") while every source hook on the same clips rendered a verdict
    ("This dog had to make the hardest decision of his life"). Each reel already
    says to match that register; this rule drowned it out.

    The beat count was never the variable either: 3+ beats rests on 45 titles
    against 354, and a one-beat title naming an idea medians 258k against 62k for
    one reporting a fact.
    """
    rules = NL.join(title_lab.production_title_rules(seed=1))

    assert "END ON A VERDICT, A TURN, OR A REACTION" in rules
    assert "Never on a description" in rules
    # beats must still be present, but subordinate
    assert rules.index("END ON A VERDICT") < rules.index("BEATS ARE HOW YOU GET THERE")
    # and the old framing must be gone, not merely supplemented
    assert "TELL IT AS A SEQUENCE, NOT A STATEMENT" not in rules
    assert "single biggest thing separating the winners" not in rules


def test_the_beats_rule_sanctions_the_single_beat_idea_examples():
    """Prose and examples must not contradict each other.

    The beats rule asks for three or more beats; several idea examples are one
    beat. Left unreconciled the model reads those examples as violations, and
    this codebase has already watched the framing win that argument: an earlier
    production block defined the job one way and demanded the opposite five
    rules later, and 18 of 18 generated options followed the framing.
    """
    rules = NL.join(title_lab.production_title_rules(seed=1))

    assert "WHEN THE CLIP HAS NO SEQUENCE, DO NOT FAKE ONE" in rules
    # and the ranking must survive: a verdict is not a licence to skip a real
    # sequence, it is what a sequence has to END on.
    assert "A one-beat verdict beats a four-beat recap" in rules
    assert "BEATS ARE HOW YOU GET THERE, NOT THE POINT" in rules


def test_every_shipping_draw_teaches_a_title_that_states_an_idea():
    """The example pools must not demonstrate narration only.

    Every line in VETTED_EXAMPLES and VETTED_MULTI_BEAT_EXAMPLES reports what
    happened in its clip. Scored on the reference set, a title that instead
    NAMES the general idea the clip is an instance of medians 258k against 62k
    at one beat and 190k against 72k at two. The rules cannot teach that on
    their own: title_lab exists on the premise that examples carry the voice.

    The measured-winner path is the one that broke it. production_title_rules
    used to request the full example_count and keep only the slots left after
    the account's own winners, and since the draw is shuffled that discarded
    the idea examples about a third of the time.
    """
    ideas = set(title_lab.VETTED_IDEA_EXAMPLES)
    winners = [f"account winner {i}" for i in range(5)]

    for seed in range(25):
        bare = NL.join(title_lab.production_title_rules(seed=seed))
        loaded = NL.join(
            title_lab.production_title_rules(few_shot_winners=winners, seed=seed)
        )
        assert any(example in bare for example in ideas), seed
        assert any(example in loaded for example in ideas), seed


def test_the_three_example_pools_stay_distinct_and_verbatim_clean():
    """Pools must not overlap, and no entry may carry OCR damage.

    The pools are quoted verbatim off OCR output on purpose, so a damaged line
    would teach broken text directly. Entries are excluded rather than
    repaired, which is why some higher-viewed members of each shape are absent.
    """
    import re

    short = set(title_lab.VETTED_EXAMPLES)
    multi = set(title_lab.VETTED_MULTI_BEAT_EXAMPLES)
    ideas = set(title_lab.VETTED_IDEA_EXAMPLES)

    assert not short & multi
    assert not short & ideas
    assert not multi & ideas

    for example in short | multi | ideas:
        assert example.isascii(), example
        assert "  " not in example, example
        # glued words ("neverseena") and tripled letters are the OCR signatures
        assert not re.search(r"[a-z]{2}[A-Z]", example), example
        assert not re.search(r"\w{17,}", example), example


def test_the_loop_rule_sizes_the_gap_rather_than_maximising_it():
    """Withholding is not the goal; a small, legible gap is.

    DO NOT CLOSE THE LOOP is a prohibition, and a batch over-corrected into
    "Kendall Jenner kept walking. Justin Bieber did not" - so much withheld that
    the reader cannot name what is missing. Loewenstein's information-gap work
    is why that fails: curiosity tracks awareness of a SPECIFIC gap, and large
    gaps produce less curiosity than small ones, because a little information is
    the priming dose. The fix is usually to give MORE away, which a bare
    prohibition would never suggest.
    """
    rules = NL.join(title_lab.production_title_rules(seed=1))

    assert "SIZE THE GAP" in rules
    assert "Mystery is not curiosity" in rules
    assert "giving MORE away, not less" in rules
    # the original prohibition must survive alongside it
    assert "DO NOT CLOSE THE LOOP" in rules


def test_an_invented_join_between_two_true_facts_is_banned():
    """STAY TRUE governs the parts, not the link between them.

    Over two clips filmed in different places a batch returned "Same sidewalk,
    two very different reactions to the people sitting on it". Both halves were
    supported and the join was invented, so STAY TRUE was never violated.
    """
    rules = NL.join(title_lab.production_title_rules(seed=1))

    assert "A RELATIONSHIP IS A SPECIFIC TOO" in rules
    assert "check that the join is evidenced, not just the parts" in rules


def test_first_person_may_react_but_never_claim_the_experience():
    """The page reposts other people's footage.

    The person rule bans first person ABOUT someone on screen but says nothing
    about claiming their day. A batch returned "My driver's profile photo was
    just a gray silhouette" over a ride somebody else filmed. Applies to both
    person_rule branches, since each permits "I", so it lives outside them.
    """
    for kwargs in ({"allow_singular_i": True}, {"allow_singular_i": False}):
        rules = NL.join(title_lab._v2_rules(**kwargs))
        assert "YOU ARE THE WATCHER, NEVER THE PARTICIPANT" in rules, kwargs
        assert "claiming you were there" in rules, kwargs


def test_the_loop_rule_reaches_every_variant_and_hands_over_no_line():
    """A title that answers its own clip leaves the video nothing to deliver.

    A real batch returned "One tap on that mystery silhouette and the driver's
    full photo pops up, turning a plain pickup into a genuine surprise" while
    the source account, over the same footage, withheld what the photo turned
    out to be. The draft was complete, informative and lost. BE CLEAR FIRST
    already ends on "dies inside a recap", but says it as an aside about
    specifics, and a line can close the loop while naming nobody - so the rule
    has to stand alone. Like the source-hook exception, it must describe the
    move without handing over a writable sentence.
    """
    rules = "\n".join(title_lab.production_title_rules(seed=1))

    assert "DO NOT CLOSE THE LOOP" in rules
    assert "the subject OR " in rules and "the outcome, never both" in rules
    # no copyable line, and no lifted fragment of the failing draft
    assert "mystery silhouette" not in rules
    assert "plain pickup" not in rules


def test_the_loop_rule_survives_the_variant_switches():
    # It is unconditional: no _v2_rules flag may drop it.
    for kwargs in (
        {"allow_singular_i": True},
        {"allow_singular_i": False, "name_recognition": True},
        {"allow_singular_i": False, "allow_emoji": True},
    ):
        assert "DO NOT CLOSE THE LOOP" in "\n".join(title_lab._v2_rules(**kwargs))


def test_different_beats_may_not_become_different_subjects():
    """Fixing the ending pushed the drift to the opening.

    Once the correction rule fixed every option's last beat, the only room left
    to differentiate was the front - so a batch opened one option on the security
    staff rather than on Neymar, reaching past the subject to fill a third slot.
    Writing two honest options has to be allowed, or the third gets invented.
    """
    rules = "\n".join(title_lab.production_title_rules(seed=1))
    assert "SAME SUBJECT THOUGH" in rules
    assert "never on a bystander, the crowd, or the staff" in rules
    assert "write two and say so in the notes" in rules


def test_a_discredited_source_loses_its_supporting_details_too():
    """"Security froze" came from the caption that invented the theft.

    The STAY TRUE rule lists the source caption as valid support, so colour from
    a caption whose headline claim is disproved still read as evidence - and the
    detail was incoherent with the correction in its own sentence: if there was
    no ring, nobody froze watching it be taken.
    """
    rules = "\n".join(title_lab.production_title_rules(seed=1))
    assert "REST OF THAT SOURCE GOES WITH IT" in rules
    assert "its supporting details are not evidence either" in rules


def test_reel_headers_carry_the_account_name_without_breaking_routing():
    """The name is a label for the operator, never a routing key.

    A cross-account batch echoes the GLOBAL item id (account-relative "#N"
    collide once a batch spans accounts), and the header regex already tolerates
    trailing text after the closing bars. So the account name can ride along to
    tell a human which chat a reply came from, while a model that paraphrases or
    drops it changes nothing about where the reply lands.
    """
    from nicheflow_studio.services import draft_handoff

    labelled = draft_handoff._batch_item_delimiter(1, 1895, "The Odd History Club")
    assert labelled == "===== REEL 1 (#1895) ===== The Odd History Club"

    match = draft_handoff._REEL_HEADER.match(labelled)
    assert match is not None
    assert match.group(1) == "1" and match.group(2) == "1895"

    # A garbled or missing name must still route on the id alone.
    for variant in (
        "===== REEL 1 (#1895) ===== the odd histry club",
        "===== REEL 1 (#1895) =====",
    ):
        again = draft_handoff._REEL_HEADER.match(variant)
        assert again is not None and again.group(2) == "1895", variant


def test_v3_allows_a_reaction_ending_where_v2_forbids_any_verdict():
    """v2 bans the filler verdict and the earned reaction in one clause.

    "telling the viewer how to feel about it" covers both, so the reference
    accounts' strongest shape (state the fact, end on a reaction only that clip
    could earn) fails v2's own read-only test. v3 keeps the recap ban and the
    filler ban, and swaps the blanket clause for a paste test.
    """
    v2 = NL.join(title_lab.production_title_rules(seed=1))
    v3 = NL.join(title_lab.production_title_rules(seed=1, reaction_ending=True))

    assert "telling the viewer how to feel about it" in v2
    assert "telling the viewer how to feel about it" not in v3

    # The two real failures the v2 A/B measured stay banned in v3.
    assert "'a genuine surprise', 'so satisfying'" in v3
    assert "recap that names the subject, the outcome and the reveal" in v3

    # The paste test is what replaces the blanket ban.
    assert "could that exact clause sit on a different clip" in v3

    # v2 keeps its withholding rule; v3 no longer asks for one (see below).
    assert "SIZE THE GAP, DO NOT JUST MAKE ONE" in v2


def test_v3_gives_the_full_context_and_never_withholds_the_key_word():
    """Short, withholding titles came out unreadable to a stranger.

    The 2026-09-23 batch shipped "This was Michael Jackson thirty six hours
    before." and "This same calm later defined his reign as king": the loop rule
    told the model to hold one thing back, and what it held back was the word
    the line needed to make sense. The reference accounts do the opposite: state
    who, what and where in full and let the footage be the payoff.
    """
    v3 = NL.join(title_lab.production_title_rules(seed=1, reaction_ending=True))

    assert "GIVE THE FULL CONTEXT" in v3
    assert "Never hold back the word that makes the line make sense" in v3
    assert "DO NOT CLOSE THE LOOP" not in v3
    assert "withhold something instead" not in v3
    assert "SIZE THE GAP" not in v3
    # Context a stranger needs is not furniture.
    assert "it is context, not furniture" in v3
    # The pick rule no longer disqualifies a title for being understandable alone.
    assert "never press play" not in v3
    assert "keeps the recognisable name and the payoff" in v3
    # v2 is untouched: its A/B history is measured under the old wording.
    v2 = NL.join(title_lab.production_title_rules(seed=1))
    assert "DO NOT CLOSE THE LOOP" in v2
    assert "never press play" in v2


def test_v3_carries_a_writable_example_nowhere_in_its_rule_text():
    """A rule that hands over a usable line gets copied verbatim.

    That already happened twice here: the source-hook rule shipped a writable
    sentence and the next batch returned it, and the old prompt's "It looked
    like" opened 50 of 360 titles. The v3 rule describes the move instead, so
    the reference titles that motivated it must not appear in the prompt.
    """
    v3 = NL.join(title_lab.production_title_rules(seed=1, reaction_ending=True))
    for borrowed in ("one tenth of his natural energy", "definition of AURA"):
        assert borrowed not in v3
