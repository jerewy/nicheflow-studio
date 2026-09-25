"""Experimental on-screen title prompts, isolated from the production path.

Nothing in the shipping draft pipeline imports this module. It exists to answer
one question on a small number of clips: does the current title prompt sound
institutional because of the model, or because of the rulebook?

The production prompt (``smart_drafts._historytrails_title_rules``) emits 20
bullets and 2,534 words to produce one line of on-screen text. Two clauses in
there are the prime suspects:

* singular first person is banned outright, so every reactive option comes back
  as "None of us expected ..." - mascot voice, not a person reacting;
* the three options must simultaneously span a register, a specificity tier and
  a word-count band, each justified against a measured median, which is a
  checklist to satisfy rather than a line to write.

The first suspect has a number behind it. Scoring the same reference set the
production rules cite (data/title_analysis/historytrails-ocr), singular "I"
appears in 35 titles including the four biggest performers in the set (4.4M,
4.0M, 2.7M, 2.3M views), while the collective "we/us/our" the prompt mandates
in its place appears in 9, whose top-ranked entries are OCR wreckage. The
shipping rulebook bans the register its own winners are written in.

So three variants run against identical evidence:

    a_current   the production rules, verbatim, as the control
    b_v2_open_i the short rulebook, first person explicitly allowed
    c_v2_no_i   the short rulebook, singular "I" still banned

C is the diagnostic. If B and C both read human the scaffolding was the problem
and the "I" ban can stay, which costs nothing; if only B lands, the freedom is
doing the work and the attribution risk has to be reviewed by hand at pick time.
Changing both at once and running two variants would leave that unanswerable.

Every variant asks for the same number of candidate lines and nothing else - no
recommended pick, no per-option rationale, no ``Title Option N:`` headers. The
paste-parser contract is a delivery concern; requiring it here would reimport
half the scaffolding under test. Porting to that format is what happens after a
variant wins.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from pathlib import Path

from nicheflow_studio.processing import smart_drafts, video

VARIANT_CURRENT = "a_current"
VARIANT_V2_OPEN_I = "b_v2_open_i"
VARIANT_V2_NO_I = "c_v2_no_i"
VARIANT_V2_NAMED = "d_v2_named"
VARIANTS = (VARIANT_CURRENT, VARIANT_V2_OPEN_I, VARIANT_V2_NO_I, VARIANT_V2_NAMED)

VARIANT_LABELS = {
    VARIANT_CURRENT: "A - current production rules (control)",
    VARIANT_V2_OPEN_I: "B - v2 short rulebook, first person allowed",
    VARIANT_V2_NO_I: "C - v2 short rulebook, singular I banned",
    VARIANT_V2_NAMED: "D - C plus recognisable names and varied length",
}

DEFAULT_CANDIDATE_COUNT = 6
DEFAULT_EXAMPLE_COUNT = 8

# The production prompt's own three-option matrix is left intact for the
# control: it asks for two independent sets of three rather than six loose
# lines, so variant A still writes under the rules it actually ships with while
# returning the same candidate count as B and C.
_CURRENT_SET_SIZE = 3


@dataclass(frozen=True)
class ClipEvidence:
    """Everything the model is told about one clip.

    Identical across variants by construction - only the rule block differs -
    because a prompt comparison where the evidence also moved proves nothing.
    """

    item_id: int | None = None
    account_name: str | None = None
    niche_label: str | None = None
    scraped_caption: str | None = None
    source_url: str | None = None
    source_description: str | None = None
    on_screen_hook: str | None = None
    transcript: str | None = None
    vision_json: str | None = None
    video_path: str | None = None
    contact_sheet: str | None = None
    # The account's own ``writing_tone`` / ``hook_style``, formatted as the
    # shipping ``build_chat_prompt`` formats them. Off by default and supplied
    # only by the A/B that asks whether those settings fight the rulebook: for
    # Past Moments Daily they read "documentary feel, factual, quietly dramatic"
    # and "full-sentence context that sets scene and stakes", which instructs the
    # exact register this rewrite moves away from. Production injects them into
    # every prompt; this bench never did, so the claim that they matter has been
    # inference rather than measurement until now.
    account_style: str | None = None

    def context_lines(self) -> list[str]:
        lines = [
            "CLIP EVIDENCE",
            f"Account: {self.account_name or '(none)'}",
            f"Niche: {self.niche_label or '(none)'}",
            f"Source URL: {self.source_url or '(none)'}",
            (
                "Scraped caption line (the reposter's descriptive prose, not their hook; "
                "context only, often narrower than the clip, never a model for the title): "
                f"{self.scraped_caption or '(none)'}"
            ),
            f"Source description: {self.source_description or '(none)'}",
        ]
        if self.account_style:
            # Placed with the account context, where build_chat_prompt puts it.
            lines[3:3] = self.account_style.splitlines()
        if self.on_screen_hook:
            lines.append(
                "Source account's own on-screen hook (the angle that earned their views; "
                "match the angle, never the wording, and do not inherit its claims unless "
                f"the evidence below supports them): {self.on_screen_hook}"
            )
        if self.contact_sheet:
            lines.append(
                f"Attach and inspect the image file {Path(self.contact_sheet).name}: it is a "
                "contact sheet of frames sampled across this clip, in order."
            )
        if self.video_path:
            lines.append(
                f'If you can open local files, inspect the video itself: "{self.video_path}"'
            )
        lines += [
            "",
            "Visual evidence JSON (from a vision pass over sampled frames; empty means none ran):",
            (self.vision_json or "").strip()[:4000] or "(none)",
            "",
            "Transcript / spoken content:",
            (self.transcript or "").strip()[:6000] or "(none)",
        ]
        return lines


def _v2_rules(
    *,
    allow_singular_i: bool,
    name_recognition: bool = False,
    vary_length: bool = False,
    allow_emoji: bool = False,
    option_spread: int = 0,
    recommend_pick: bool = False,
    reaction_ending: bool = False,
) -> list[str]:
    """The short rulebook.

    Five constraints survive the cut, each because it fixes an observed failure
    rather than because it encodes a measured median:

    * SCOPE - a compilation of three psychiatrists came back as a title about
      one psychiatrist, throwing away the pattern that was the whole clip;
    * DO NOT CLOSE THE LOOP - a batch came back with titles that answered their
      own clips, and two of them lost a direct A/B against the source account's
      hook over the same footage (see the rule for the case);
    * STAY TRUE - an earlier rule demanded a specific the footage did not have
      and got "a room full of psychiatrists" over two people interviewed apart;
    * NEVER - the user's standing ban on em dashes and AI-tell phrasing;
    * examples are structure only - facts leaked between clips without it.

    Everything else the production block spends words on (specificity tiers,
    length bands, register mixes, measured medians, ranked shape lists) is left
    out. Models copy examples far more reliably than they follow prose rules -
    ``title_references`` says as much in its own docstring - so the examples are
    asked to carry the voice and the prose is asked to carry almost nothing.
    """
    person_rule = (
        "- First person is allowed and is often the strongest choice: 'I have never seen "
        "anything more accurate', 'nothing has made me rethink my week this fast'. Write "
        "it the way you would actually say it out loud."
        if allow_singular_i
        else "- FIRST PERSON, WITH ONE LINE YOU DO NOT CROSS. Write as the person who "
        "just watched this: 'I had never heard that...', 'I still love this crossover', "
        "'come back to this when you need the courage'. That is the account's own "
        "highest-reach register and it is welcome. What is never allowed is first "
        "person about a PERSON on screen, their looks, their body, or wanting them: "
        "'I would let her tell me a bedtime story any day' over a swimsuit clip is the "
        "line that fails, and it fails no matter how clever the wordplay is. React to "
        "what happened, never to who you find attractive. Collective we, us and our are "
        "fine anywhere."
    )
    return [
        "- GOAL: write the line that sits over the clip. It makes a stranger stop, "
        "watch to the payoff, and want to send it to someone.",
        "- VOICE: you just watched this and you are showing it to a friend. Not "
        "narrating it, not introducing it, not labelling it. If it sounds like a brand "
        "account wrote it, it is wrong.",
        person_rule,
        # The branch above governs first person ABOUT someone on screen. It has no
        # answer for first person claiming the EXPERIENCE, and a batch returned "My
        # driver's profile photo was just a gray silhouette. I tapped it anyway" over
        # a clip somebody else filmed of their own ride. The v1 rules caught this
        # ("'I was just offered a quarter million' reads as the page being offered
        # it"); v2's narrower wording does not.
        "- YOU ARE THE WATCHER, NEVER THE PARTICIPANT. First person is for reacting to "
        "footage ('I had never heard that', 'I still love this crossover'), never for "
        "claiming you were there. This page reposts other people's clips, so 'my "
        "driver', 'my flight', 'my dog' claims a day that belonged to someone else and "
        "the caption underneath will contradict it. If the moment is theirs, write "
        "about them; keep the first person for your reaction to watching it.",
        (
            # C's rule pushed away from names hard enough that across two clips the
            # model almost never used one, while the account's own scoring says a
            # specific inside a REACTIVE line beats the same specific in a flat
            # recap. The distinction is recognition versus furniture, not presence
            # versus absence, so D states that instead of a blanket discouragement.
            "- BE CLEAR FIRST: a stranger who knows nothing should understand what "
            "they are looking at, and naming a person, film, show or moment they "
            "would recognise is one of the best ways to do that. Name them when the "
            "recognition is part of the draw. A year works the same way when the "
            "distance in time is the point. What does not work is furniture: a "
            "venue, an event name, or a date that only says where and when this "
            "happened. A specific lives inside a reaction and dies inside a recap."
            + (
                # v3 only. "Furniture" was read as licence to cut the show, the
                # place or the event even when the line made no sense without it.
                " But if a stranger needs it to understand the clip (the show it "
                "aired on, the place, the event), it is context, not furniture: keep it."
                if reaction_ending
                else ""
            )
            if name_recognition
            else "- BE CLEAR, NOT SPECIFIC: a stranger who knows nothing about the "
            "subject should understand what they are looking at. A name, year, place "
            "or number goes in only when that fact IS the hook. Otherwise leave it out."
        ),
        # A recap reads as a finished, informative sentence, which is exactly why
        # this failure is quiet: nothing about the output looks wrong. Measured
        # the only way that matters, against the source account's own hook over
        # the SAME footage: on a rideshare clip the source withheld what the
        # photo turned out to be and the draft stated it, then told the viewer it
        # was a surprise; on a former child star's clip the source withheld who
        # he was and the draft named the show, the character, the years, the
        # outcome and who stepped in. Both drafts were complete and both left the
        # video with nothing to deliver. The existing BE CLEAR rule ends on this
        # ("a specific lives inside a reaction and dies inside a recap") but says
        # it as an aside about specifics, and a title can close the loop while
        # naming nobody at all, so it needs saying on its own.
        #
        # Deliberately no example sentence: see the source-hook exception above
        # for what happens when a rule hands over a writable line.
        (
            # The v2 rule bans the recap AND the filler verdict in one clause
            # ("telling the viewer how to feel about it"), and the reference
            # accounts live in the gap between them: they state the fact plainly
            # and end on a reaction that no other clip could earn. Measured on
            # @historytrails and @insidehistory grids, that shape carries their
            # best posts, and v2's own test ("did they miss anything?") rejects
            # every one of them. The A/B that produced the v2 rule is not
            # contradicted: both losing drafts ended on something that fits any
            # clip, which is what the paste test below still throws out.
            # v3 used to open "DO NOT CLOSE THE LOOP" and fall back to withholding,
            # and on the 2026-09-23 batch the withheld piece was the word the line
            # needed: "This was Michael Jackson thirty six hours before." The
            # reference accounts state who, what and where in full; the footage is
            # the payoff, so the title does not have to hide anything to earn a play.
            "- GIVE THE FULL CONTEXT, LET THE CLIP BE THE PAYOFF. A stranger reading "
            "only your line must understand who or what this is and why it is worth "
            "watching. Never hold back the word that makes the line make sense: the "
            "name, the place, the event, or what happened next. The viewer still "
            "presses play, because your line tells them what they are about to see "
            "and the footage shows it. Two things still kill a title. A recap that "
            "names the subject, the outcome and the reveal together and describes "
            "every beat leaves the footage nothing to show. A verdict that would fit "
            "any clip ('a genuine surprise', 'so satisfying') adds nothing this clip "
            "earned. If you end on a reaction, point it at the specific thing in this "
            "footage, and test it: could that exact clause sit on a different clip? "
            "If yes, cut it and end on the fact."
            if reaction_ending
            else "- DO NOT CLOSE THE LOOP: after your line, the viewer must still "
            "need the clip. Stating the outcome, naming the reveal, or telling the "
            "viewer how to feel about it ('a genuine surprise', 'so satisfying') "
            "leaves the footage nothing to pay off. Withhold the ONE thing the clip "
            "reveals, the subject OR the outcome, never both, and let the last clause "
            "point at it without saying it. The test is a question you ask yourself "
            "after writing: if someone read only this line and never pressed play, "
            "did they miss anything? If not, rewrite it."
        ),
        # The rule above is a prohibition, and a prohibition invites over-correction:
        # a batch answered it with "Kendall Jenner kept walking. Justin Bieber did
        # not", which withholds so much that the reader cannot tell what is missing.
        # Loewenstein's information-gap work is the reason that fails. Curiosity
        # tracks awareness of a SPECIFIC gap, and the size relationship runs backwards
        # from intuition: large gaps produce LOW curiosity, small ones high, because a
        # little information is the priming dose that makes the hole feel worth
        # closing. Golman and Loewenstein add that the reader must also believe
        # finding out is possible and that the answer matters. So withholding is not
        # the goal and more of it is not better.
        (
            "- VAGUE IS NOT INTRIGUING. A line a stranger has to decode reads as "
            "vague and they scroll. Before you keep a line, check: could someone who "
            "has never seen this clip say what it is about from your words alone? If "
            "not, add the missing context. The fix is almost always giving MORE away, "
            "not less."
            if reaction_ending
            else "- SIZE THE GAP, DO NOT JUST MAKE ONE. Mystery is not curiosity. A "
            "reader is pulled in when they can see EXACTLY what they are missing and "
            "believe the clip will supply it; a line that hides so much they cannot "
            "name the missing piece reads as vague, not intriguing, and they scroll. "
            "So give the setup, the expectation, and enough context that the hole has "
            "a shape, then withhold one precise thing. Three checks before you keep a "
            "line: can the reader say what they do not know, will the clip obviously "
            "answer it, and would they care about the answer? A no to any of the "
            "three means the gap is the wrong size, and the usual fix is giving MORE "
            "away, not less."
        ),
        "- SCOPE: cover as much of the clip as the clip covers. If the footage shows "
        "three people each failing at the same thing, the line is about all three, not "
        "about the first one.",
        "- STAY TRUE: every specific must be supported by the transcript, the caption, "
        "or what is visible. If a detail is not supported, soften it rather than invent "
        "it. No first, no record, no rarity claim the evidence does not back.",
        # STAY TRUE governs the PARTS and a batch slipped straight through it: over
        # two clips filmed in different places it returned "Same sidewalk, two very
        # different reactions to the people sitting on it". Both halves were true and
        # the join was invented, so nothing in the rule above was violated.
        "- A RELATIONSHIP IS A SPECIFIC TOO. Two facts can each be supported while the "
        "link you draw between them is not. 'Same street', 'moments later', 'right "
        "after', 'which is why', 'the same day' all assert something the evidence has "
        "to carry, and a clip cut from two separate sources carries none of it. Before "
        "you join two things, check that the join is evidenced, not just the parts. "
        "When it is not, say what each shows and let the reader draw the line.",
        # Without this the two instructions around the source hook deadlock. The
        # evidence block says to MATCH THE ANGLE of the hook that earned the
        # original views, and STAY TRUE says not to inherit its unverified
        # claims - but on a clip like "Bro already has 10 years of experience
        # after 1 week on the job" the angle IS the claim. A real batch resolved
        # that by dropping "first week" entirely, which threw away the joke and
        # left a title about a man driving competently.
        # This used to be an exception that let the hook's claim stand as the
        # framing when the evidence was silent. That is an unchecked claim stated
        # as fact, and chat assistants refused whole batches over it once the OCR
        # pass filled the hook on most reels. The deadlock is resolved the other
        # way: keep the angle's energy, drop the claim.
        # Describe the move, never hand over a line that fits the clip in front
        # of it: a writable example here once came back near-verbatim.
        "- THE SOURCE'S OWN ON-SCREEN HOOK IS NOT EVIDENCE: when the angle that earned "
        "the original views is itself a claim ('a week into the job', 'his first ever "
        "attempt') and nothing in the evidence supports it, do not state it as fact. "
        "Keep what makes the angle work, the surprise or the joke, and anchor it to "
        "what the clip visibly shows. When the evidence CONTRADICTS the claim, the "
        "rule below takes over.",
        # A real batch researched the Neymar ring theft, found he had denied it and
        # was not wearing a ring, put the correction in one option, and then wrote
        # two more asserting the theft as fact - because the different-beats rule
        # asked for three angles and two of the available angles were the false
        # version. Whichever of those two got picked would have shipped it.
        "- WHEN THE EVIDENCE KILLS THE PREMISE, THE CORRECTION IS THE LAST BEAT OF "
        "EVERY OPTION. If research or the evidence shows the story everyone is sharing "
        "did not happen, do not quietly drop the clip and do not write around it, and "
        "never let one option carry the correction while the others still state the "
        "claim as fact. Lead each option with what people believe and turn it on what "
        "is actually true. The false version is the setup, the correction is the "
        "payoff, and that is the strongest shape available to you. Two conditions: the "
        "turn must be the LAST thing the line says, so a half-read title does not "
        "assert the false version, and the correction has to be genuinely interesting. "
        "If it is not, the clip is not worth drafting and the niche check should say "
        "so. AND THE REST OF THAT SOURCE GOES WITH IT: once a caption's headline claim "
        "is shown to be false, its supporting details are not evidence either. They were "
        "written to sell the same story. A caption that invented a theft also invented "
        "the security staff freezing as it happened, and a detail that only makes sense "
        "if the false version is true cannot appear in a title that ends by saying it is "
        "not.",
        (
            "- NEVER: em dash, en dash, double hyphen, "
            + ("" if allow_emoji else "emoji, ")
            + "hashtag, all-caps line. Never 'shocking', 'you won't believe', 'this will "
            "blow your mind', 'changed history forever', 'not just X but Y', 'in a world "
            # The second group all shipped in the 2026-09-14 batch: stock phrases
            # that stand in for the reaction instead of stating it.
            "where', 'says it all', 'went viral', 'more than words ever could', 'broke "
            "the internet', 'broke the room'. No exclamation-point selling."
        ),
        *(
            # The renderer already supports this properly: a title carrying an
            # emoji switches off ffmpeg drawtext (one font, no fallback, so emoji
            # would come out as missing-glyph boxes) and renders through Pillow
            # with embedded_color=True against Segoe UI Emoji, giving real colour
            # glyphs composited over the footage. Non-Windows strips them instead.
            #
            # Whether emoji HELP is unmeasured and cannot be measured from the
            # reference set: the OCR extractor does not read emoji glyphs, so all
            # 399 scored titles show none while the source account demonstrably
            # uses them. This rule is modelled on how the source uses it - as a
            # register marker telling you to read the line as a joke - not on data.
            [
                "- ONE EMOJI IS ALLOWED, at the very end of the line, and only when it "
                "marks how the line should be read: that this is a joke, a wince, a "
                "shock. The accounts you are learning from use it exactly that way "
                "('...after 1 week on the job \U0001f62d'). Never decorative, never more "
                "than one, never mid-sentence, and never doing work the words should do. "
                "If the line only lands because of the emoji, the line is not finished. "
                "Most lines should still have none. "
                # Restricted to the set this account actually uses. Left open, a
                # model reaches for the generic engagement-bait palette (fire,
                # mind-blown, hundred points), which is the register the rest of
                # this prompt spends its length avoiding. Every one of these is
                # verified to render as a full colour glyph through the burn-in
                # path rather than a missing-glyph box.
                "Use ONLY these, nothing else: "
                "\U0001f62d \U0001f979 \U0001f480 \U0001f602 \U0001f605 \U0001f972"
            ]
            if allow_emoji
            else []
        ),
        "- The examples below are real titles that performed on this kind of account. "
        "Copy their voice and their shapes. Never copy their words, and never carry a "
        "fact from one of them onto this clip.",
        *(
            # Each option leads with a DIFFERENT beat. Without this the options come
            # back as one hook at three word counts - measured across a real batch,
            # the Cupid clip returned "not one chair turned", "nobody turned their
            # chair" and "zero chairs, and CeeLo still asks", which is one option
            # wearing three coats and gives a reviewer nothing to choose between.
            #
            # The SAME SUBJECT half was added after the correction rule below took
            # away the room these options had to differ at the END. With the
            # ending fixed, they began differing at the front instead, and reached
            # past the subject to do it: one batch opened an option on the
            # security staff rather than on Neymar, using a detail supplied by the
            # very caption that had invented the theft.
            [
                f"- THE {option_spread} OPTIONS MUST START FROM DIFFERENT BEATS, not the "
                "same beat reworded at different lengths. Pick the clip's beats first, "
                "then lead each option with a different one and let the rest follow. For "
                "a clip about a singer whose audition got no chair turns, that is: one "
                "option leading on the song he chose, one leading on the judge's "
                "reaction, one leading on the empty chairs. Same story, three ways in. "
                "If two of your options could be swapped without changing what the "
                "viewer learns first, you have written one option twice. "
                "SAME SUBJECT THOUGH: the options differ in which beat they open on, "
                "never in what the clip is about. Keep the main subject in view in every "
                "one. A clip about a named person opens on that person or on what "
                "happened to them, never on a bystander, the crowd, or the staff "
                "standing nearby, however convenient that is for making the third option "
                "look different. If the clip only honestly supports two openings, write "
                "two and say so in the notes rather than reaching past the subject to "
                "fill a third slot."
            ]
            if option_spread > 1
            else []
        ),
        *(
            # The shipping prompt asks for "Recommended Pick: Title Option N", so
            # without this the model is asked to choose with no basis given. The
            # old block ranked by specificity against a measured median; this
            # ranks by what actually went wrong in testing. Across one measured
            # batch, 5 of 18 options carried a claim nothing in the evidence
            # supported, and the model's OWN recommendation landed on two of
            # them - so eligibility has to come before quality.
            # Eligibility still comes first, but on its own it became the whole
            # decision: across the 2026-09-14 batch nearly every Why justified the
            # pick by support ("without overstating"), and the picks included pure
            # narration ("the clouds break, the wing drops...") and a sentence that
            # did not parse, while a verdict option on the same clip sat unpicked.
            # So support is stated as a gate and the choice among eligible options
            # is spelled out as checks run in order, each of which disqualifies.
            [
                "- RECOMMENDED PICK, IN THIS ORDER. (1) Rule out any option that states "
                "something the transcript, the source caption, the source hook, or the "
                "frames do not support. This is a gate, not the reason to pick: every "
                "option left has passed it, so 'it is supported' says nothing about which "
                "is best. (2) Rule out any option that does not read cleanly in one "
                "breath: a run-on joined by a comma, a sentence that breaks off or does "
                "not parse, a name or reference a stranger cannot place. (3) Rule out any "
                "option whose last clause describes a step in the action or restates the "
                "setup, "
                + (
                    # v3: the old "never press play" test disqualified exactly the
                    # full-context titles the context rule now asks for.
                    "any option that narrates every beat of the clip, and any option a "
                    "stranger could not understand without seeing the clip. (4) Among "
                    "what survives, prefer the one that keeps the recognisable name and "
                    "the payoff, and take the one a stranger would most want to send to "
                    "someone. Do NOT pick an option for being the most "
                    if reaction_ending
                    else "and any option that passes the 'read only this line and never "
                    "press play' test. (4) Among what survives, take the one a stranger "
                    "would most want to send to someone. Do NOT pick an option for being "
                    "the most "
                )
                +
                "factual, the most concrete, or the safest: density is not quality and "
                "hedging is not a virtue. The Why must say what this option does better "
                "than the others, not that it is supported."
            ]
            if recommend_pick
            else []
        ),
        *(
            # Removing the production prompt's 18-24 word band removed long titles
            # entirely: across 24 v2 candidates on two clips, nothing ran past 16
            # words and the median sat at 9-12, which is the account's WORST
            # measured band. Stated as a spread rather than a target, because the
            # numeric band was scaffolding and the collapse to short is the actual
            # failure being fixed.
            [
                # Deliberately NOT a word count, and not a spread either. Two
                # attempts at legislating length went nowhere useful: "full
                # sentences" is a grammatical property a 7-word line satisfies, and
                # an explicit band is what the production prompt uses to force a
                # median of 24 whether or not the clip earns it. The account owner's
                # call is that length should follow the clip, so the rule names the
                # CONDITION that makes a long line correct rather than a target.
                # This bullet was 1,771 characters and opened by calling beats "the single
        # biggest thing separating the winners from the rest". It is the loudest
        # rule in the block, and what it actually taught was NARRATION: eight reels
        # in one batch came back describing what happens ("He walked a few steps,
        # stopped, turned back toward her laugh") while every source hook on the
        # same clips rendered a verdict ("This dog had to make the hardest decision
        # of his life"). Each reel's context already says to MATCH THE REGISTER of
        # that hook, verdict included, and this rule drowned it out - the same way
        # an earlier production block's opening framing beat a later rule 18 times
        # out of 18.
        #
        # The beat count was also never the variable. 3+ beats rests on 45 titles
        # against 354, while a ONE-beat title naming an idea measures 258k against
        # 62k for one reporting a fact - comparable to the whole 3+ column on a
        # bigger sample. "That time Nancy Sinatra nearly lost her deal" and "This is
        # why mercury is not allowed on airplanes" are both one beat and both
        # winners. So the split is verdict-or-turn versus description, and the rule
        # now leads with that and mentions beats second.
        "- END ON A VERDICT, A TURN, OR A REACTION. Never on a description. This is "
        "the line between the winners and the rest: a title that says what the "
        "moment WAS beats one that reports what happened in it, and reporting is "
        "the default you have to fight. 'He walked a few steps, stopped, and turned "
        "back' narrates; 'that dog had to make the hardest decision of his life' "
        "renders a verdict on the same footage. Write the second kind. Check your "
        "last clause: if it is a step in the action rather than a judgment, a "
        "reveal, or someone's reaction, you have written a summary.",
        "- BEATS ARE HOW YOU GET THERE, NOT THE POINT. When the clip has a real "
        "sequence, walking through it earns the ending: setup, what happened, then "
        "the turn. The strongest turn reverses the setup ('turns out', 'but', "
        "'until', 'it was never really'). Never manufacture one, and never chain "
        "steps the clip did not earn, because three invented beats are worse than "
        "one true line. WHEN THE CLIP HAS NO SEQUENCE, DO NOT FAKE ONE: name the "
        "general idea it is an instance of and let the footage be the evidence "
        "('Once buddies, always buddies', 'Mathematics can sometimes be incredibly "
        "surprising'). A one-beat verdict beats a four-beat recap every time.",
        "- LENGTH IS FREE, WITH ONE HARD CEILING: 130 CHARACTERS including "
                "spaces. Count them. This is not a style preference and not a "
                "performance target, it is the frame: the title is burned into the video "
                "across at most four lines, and past this it runs off the side and the "
                "export is unusable. Long words hit the wall sooner than the count "
                "suggests. If the beats will not fit, cut the WEAKEST beat, never the "
                "last one, because the last beat is the payoff. Below that ceiling there "
                "is no target in either direction: write the beats the clip has and "
                "stop. Never pad to seem substantial, and never chain beats the clip did "
                "not earn, because three invented steps are worse than one true one."
            ]
            if vary_length
            else []
        ),
    ]


def _rules_for(variant: str) -> list[str]:
    """Which rule flags each variant turns on.

    Single source of truth because there are two callers - the single-clip
    prompt and the batch prompt - and they drifted the moment a flag was added
    to one of them. A variant whose batch prompt carries different rules from
    its single-clip prompt makes every comparison between the two meaningless.
    """
    return _v2_rules(
        allow_singular_i=variant == VARIANT_V2_OPEN_I,
        name_recognition=variant == VARIANT_V2_NAMED,
        vary_length=variant == VARIANT_V2_NAMED,
        # Off by decision, not by limitation. The burn-in path renders these as
        # real colour glyphs, but it rasterises them on the Windows machine doing
        # the export using the only emoji font Windows ships (Segoe UI Emoji), so
        # the artwork baked into the frame is the Windows drawing rather than the
        # Apple or WhatsApp one the account owner sees when typing. A caption
        # emoji stays as text and each viewer's phone draws it natively; a title
        # emoji is pixels the moment it is encoded. Flip this back on only
        # alongside a bundled Noto or Twemoji font, which is what would make the
        # burned-in artwork match what people expect on a phone.
        allow_emoji=False,
    )


def production_title_rules(
    *,
    option_count: int = 3,
    seed: int | None = None,
    example_count: int = DEFAULT_EXAMPLE_COUNT,
    few_shot_winners: list[str] | tuple[str, ...] | None = None,
    reaction_ending: bool = False,
) -> list[str]:
    """The v2 on-screen title rules, for the shipping batch prompt.

    The one public entry point ``draft_handoff`` uses, so the app's prompt and
    the bench's prompt can never describe different rules. Returns the rule
    bullets plus the worked-example block, in the order they should be emitted;
    the caller owns the surrounding headers and the return format.

    ``few_shot_winners`` are THIS account's own posted titles ranked by measured
    reach (``post_metrics.top_titles_for_account``). They lead the example block
    and the vetted pool only tops up the remainder, because they beat the vetted
    set on every axis that matters: same account, same audience, real reach
    numbers, and clean text the operator wrote rather than OCR output that is
    roughly 40% unusable at the top end. They also already show the structure
    these rules ask for - "From a distance, it looks like an easy meal standing
    at the water's edge. It is actually a..." is a reversal across two beats.

    That rests on ``top_titles_for_account`` ranking conversion INSIDE the
    account's most-reached posts (see ``post_metrics.WINNER_REACH_POOL``). While
    it ranked across every post, a reel seen by 1,655 people led this block on an
    account whose best clears a million, and the examples taught whatever that
    reel happened to say.

    An account with no metrics yet falls back to the vetted pool alone.
    """
    measured = [
        cleaned
        for value in (few_shot_winners or ())
        if (cleaned := " ".join(str(value).split()))
    ][:example_count]
    # Ask for the slots actually left, not the full count. Requesting 8 and
    # keeping 3 threw away the stratification: the draw is shuffled, so with 5
    # measured winners the two idea-stating examples were dropped roughly a
    # third of the time and the shipping prompt demonstrated narration only.
    top_up_slots = max(0, example_count - len(measured))
    topped_up = [
        title
        for title in select_examples(count=top_up_slots, seed=seed)
        if title not in measured
    ][:top_up_slots]
    return [
        *_v2_rules(
            allow_singular_i=False,
            name_recognition=True,
            vary_length=True,
            allow_emoji=False,
            option_spread=option_count,
            recommend_pick=option_count > 1,
            reaction_ending=reaction_ending,
        ),
        *_example_block(measured + topped_up, measured_count=len(measured)),
    ]


def _example_block(titles: list[str], *, measured_count: int = 0) -> list[str]:
    if not titles:
        return []
    header = (
        "REAL TITLES THAT PERFORMED (structure and voice only, never their facts). "
        "The first few are THIS account's own best-performing titles, ranked by reach:"
        if measured_count
        else "REAL TITLES THAT PERFORMED ON THIS KIND OF ACCOUNT (structure and voice "
        "only, never their facts):"
    )
    return ["", header, *(f"- {title}" for title in titles)]


# Hand-vetted from the scored reference set (data/title_analysis/historytrails-ocr),
# taken verbatim off the highest-viewed question and first-person titles. Nothing
# here is repaired or rewritten: every line is exactly as it was captured, so an
# example can never assert something the account did not actually publish. The
# lowercase "i'm" in two of them is how the real title was styled.
#
# Curated rather than sampled, which is a deliberate reversal of what
# title_references does for production, for two reasons.
#
# First, quality. Once the prose scaffolding is gone the examples carry the whole
# teaching load, and roughly 40% of the top slice of each bucket is OCR wreckage
# ("I've neverseena anything more accurate", "Scientists did a brain scan ofa a
# catto"). title_references' segmentation heuristics cannot catch a glue of two
# short common words without a dictionary, and a broken example now teaches
# broken text directly.
#
# Second, scope. Rotation exists in production to stop a fixed example set
# becoming a fixed attractor across hundreds of generations. A three-variant
# bench over a handful of clips has no such exposure, and example quality is part
# of what is being tested. Porting a winning variant to the paste path means
# restoring rotation over a cleaned reference set, not shipping this constant.
VETTED_EXAMPLES = (
    "That might be the filthiest pool shot I've ever witnessed.",
    "Nothing made me rethink my entire life the way Men in Black's ending did",
    "And I've been using them wrong my whole life",
    "I loved this crossover",
    "I'll never get tired of watching this video",
    "After 16 years, this is just as funny as it was the first time I saw it.",
    "She definitely did that on purpose i'm in tears",
    "Christina Koch greeting her dog after returning from the moon, i'm gonna cry",
    "come back to this video when I need the courage to take a chance",
    "Michael Jackson rehearsing after the Pepsi incident is incredible, how does "
    "he still move with such precise accuracy?",
    "Remember these kids? This is them now",
    "Hold up... Did you see that?",
    "Who designed this??",
    "It's 2026 and we still don't have this technology",
    "How we browsed the internet in 2000",
)

# Titles that move through THREE OR MORE BEATS - setup, what happened, the turn,
# someone's reaction - rather than stating one thing. This is the largest effect
# measured on the reference set and it survives controlling for length:
#
#     words     1-2 beats            3+ beats
#     1-13      n=202   63,638       n= 7  193,491
#     14-19     n=102   73,426       n= 7  150,664
#     20-26     n= 38   62,854       n=14  352,260
#     27+       n= 12   64,492       n=17  153,991
#
# Read the left column: 63k, 73k, 63k, 64k. A one-or-two-beat title performs the
# same at 8 words as at 30. Length was never the variable - beats were, and long
# titles only win when the extra words buy extra beats. That is why every length
# rule written for this prompt moved the median by nothing: they asked for words.
#
# This pool exists because the examples were teaching the opposite. Of the 15
# above, ZERO have three beats and the median is one, so 15 worked examples were
# demonstrating single-beat statements while the prose asked for stories. An
# earlier version of this pool selected for LENGTH instead, and half its entries
# were long single-beat lines, which the table above says are worth nothing.
VETTED_MULTI_BEAT_EXAMPLES = (
    "Tim Conway fell faster in rehearsal, so Harvey Korman and Carol Burnett lost it "
    "when he slowed it down on tape",
    "When Kevin Hart seriously pressed Rakai for flirting with his daughter, told him "
    "to stay far away... and then dropped the wildest line at the end",
    "Almost hit by a car... then the universe dropped free food. Legend",
    "He has never been so happy cleaning windows until this moment.",
    "When Jack Black tried to shoo away the paparazzi while he was at the beach but "
    "ended up running off after they told him they were filming him",
    "She thought her 19-year-old cat had quietly passed away in her sleep, but the "
    "moment she felt her heartbeat, tears of relief filled her eyes.",
)


# Titles that name a GENERAL IDEA the clip is an instance of, rather than
# reporting what happens in it. The title makes the claim; the footage is the
# evidence for it. Statistics alone never surfaced this: the shape has no common
# opener, word count or proper-noun signature, and "Once buddies, always
# buddies" shares no surface feature with "This is how Michael Jackson defied
# gravity". It only appears when the titles are read as writing.
#
# Scored against views, and controlled for beats because the pool below shows
# beats are the confounder that matters:
#
#     beats   idea-stating          narrating
#     1       n=22    258,060       n=379    61,663
#     2       n=15    189,866       n=192    71,939
#     3+      n= 5     76,518       n= 56   518,585
#
# Read the last row. The effect is 4.2x at one beat, 2.6x at two, and REVERSES
# once a title carries three. Naming an idea is a SUBSTITUTE for telling a
# story, not a garnish on one: a clip with a real three-beat story should tell
# it, and framing that story abstractly measured worse than just telling it.
# So this pool and VETTED_MULTI_BEAT_EXAMPLES teach alternatives, and a title
# should reach for one or the other, never both. The 3+ cell rests on 5 titles
# and is the weakest number here; the 1- and 2-beat rows are the load-bearing
# ones.
#
# Same verbatim rule as the pools above: every line is exactly as captured. That
# is why several higher-viewed members of this shape are absent - "Japanese
# people trying to pronounce refrigerator" will make your day' carries a stray
# OCR quote mark, and repairing it would teach invented punctuation.
VETTED_IDEA_EXAMPLES = (
    "Once buddies, always buddies",
    "The level of disrespect was crazy.",
    "This is how Michael Jackson defied gravity",
    "This explains why some perfumes last longer than others...",
    "The practical intelligence of this horse that was tired of being kicked.",
    "Mathematics can sometimes be incredibly surprising.",
    "This is why mercury is not allowed on airplanes",
    "The true meaning of seizing an opportunity. It feels like she's been "
    "waiting her whole life for this moment.",
    "Kids today will never understand this",
    "This is how much a laptop cost in 1989",
    "No matter how much you train or prepare, sometimes fate has different "
    "plans for who comes out on top.",
    "This is not just a system, this is pure respect for humanity.",
    "Never forget the moment Gordon Ramsay tried Southern soul food and "
    "returned to the kitchen with a completely clean plate.",
    "This will never get old",
)


def select_examples(*, count: int = DEFAULT_EXAMPLE_COUNT, seed: int | None = None) -> list[str]:
    """A seeded slice of the vetted examples.

    Identical for variants B and C on purpose. C exists to isolate ONE clause -
    the singular-"I" ban - so its examples have to match B's exactly; swapping in
    a collective-only set would move two things at once and make the comparison
    unreadable, which is the mistake C was added to avoid.

    The contradiction that leaves in C (examples written in a voice its rule
    forbids) is not an artifact of this bench. It is what production does today:
    ``title_references`` classifies a title as first-person on i/my/me/we/our/us
    alike, so the shipping prompt already rotates in singular-"I" examples under
    a rule banning them.
    """
    if count <= 0:
        return []
    rng = random.Random(seed)
    # Stratified, not a flat sample: a random draw can easily come back with no
    # multi-beat lines at all, and a single-beat teacher is what produced titles
    # that state an outcome and stop.
    # Half the slots, not a third: this is the biggest effect measured here, and
    # the examples are what teach it.
    beat_slots = max(1, count // 2)
    # The idea pool gets guaranteed slots for the same reason the beat pool does.
    # Neither of the other two teaches it: every line in VETTED_EXAMPLES and
    # VETTED_MULTI_BEAT_EXAMPLES reports what happened in its clip, so a draw
    # without this stratum demonstrates narration only and the model narrates.
    # A quarter rather than a half: the two shapes are alternatives (see the
    # pool's beat table), and beats remain the larger measured effect.
    idea_slots = max(1, count // 4) if count >= 2 else 0
    beat_slots = max(1, min(beat_slots, count - idea_slots))
    short = list(VETTED_EXAMPLES)
    multi = list(VETTED_MULTI_BEAT_EXAMPLES)
    ideas = list(VETTED_IDEA_EXAMPLES)
    rng.shuffle(short)
    rng.shuffle(multi)
    rng.shuffle(ideas)
    picked = (
        ideas[:idea_slots]
        + multi[:beat_slots]
        + short[: max(0, count - beat_slots - idea_slots)]
    )
    rng.shuffle(picked)  # so the long ones are not all clustered at the top
    return picked


def _current_rules(evidence: ClipEvidence) -> list[str]:
    """The production title rules, pulled live rather than copied.

    Imported from ``smart_drafts`` so the control cannot drift from what the app
    actually ships. Read-only: this calls the rule builder, it does not change
    it. ``title_length="long"`` matches what the chat handoff passes today.
    """
    return smart_drafts.effective_title_rules(
        None,
        None,
        evidence.niche_label,
        title_length="long",
    )


def build_prompt(
    evidence: ClipEvidence,
    *,
    variant: str,
    count: int = DEFAULT_CANDIDATE_COUNT,
    seed: int | None = None,
    example_count: int = DEFAULT_EXAMPLE_COUNT,
) -> str:
    """Render one variant's copy-paste prompt for a single clip."""
    if variant not in VARIANTS:
        raise ValueError(f"Unknown variant {variant!r}; expected one of {VARIANTS}.")

    header = [
        "Write on-screen title options for ONE short-form video clip.",
        "",
        *evidence.context_lines(),
        "",
    ]

    if variant == VARIANT_CURRENT:
        sets = max(1, round(count / _CURRENT_SET_SIZE))
        rules = _current_rules(evidence)
        ask = [
            f"Produce {sets} INDEPENDENT sets of the three options described above, "
            f"{sets * _CURRENT_SET_SIZE} lines in total. Each set follows the rules on its "
            "own; the sets should not repeat each other.",
        ]
        examples: list[str] = []  # the production rules carry their own examples
    else:
        rules = _rules_for(variant)
        ask = [
            f"Write {count} lines. They should not be rewrites of each other.",
        ]
        examples = _example_block(select_examples(count=example_count, seed=seed))

    return "\n".join(
        [
            *header,
            "RULES",
            *rules,
            *examples,
            "",
            *ask,
            "",
            "RETURN FORMAT: the lines themselves, one per line, nothing else. No "
            "numbering, no headers, no quotes, no recommended pick, no explanation of "
            "any kind. A line of prose that is not a candidate title breaks the import.",
        ]
    )


# Length is checked AFTER generation, never requested during it. Measured on the
# reference set, median views by word count run 53k (1-5), 87k (6-9), 62k
# (10-13), 74k (14-17), 86k (18-21), 101k (22-27), 77k (28+): flat noise from 6
# to 27 with one real signal, that very short titles are the weakest band. So
# length does not predict reach and does not belong in the prompt.
#
# What DOES bite is the renderer. ``_fit_title_overlay`` shrinks the font to a
# floor of 24 and then allows four lines; past that it neither truncates nor
# raises, it falls back to a greedy wrap that dumps every remaining word onto the
# final line. A 58-word title renders as three normal lines plus one of 258
# characters running off the side of the frame, and that is only visible at
# export.
_MIN_USEFUL_WORDS = 6
# Mirrors the two sizing constants inside ``_fit_title_overlay``. Duplicated
# rather than imported because that function returns the fitted result and not
# the budget it fitted against; ``tests/test_title_lab.py`` pins the pairing so a
# change to the renderer's constants fails there instead of silently loosening
# this check.
_OVERLAY_FONT_FLOOR = 24
_OVERLAY_BASE_FONT = 41


def check_length(title: str, *, crop_width: int = 1080) -> str | None:
    """``None`` when the line is fine, otherwise why it is not.

    Asks the real renderer rather than counting words, so the answer is what the
    export will actually do rather than a rule of thumb about it.
    """
    words = len(title.split())
    if words < _MIN_USEFUL_WORDS:
        return f"{words} words: the 1-5 word band is the weakest measured on this account"

    font_size, wrapped, _ = video._fit_title_overlay(
        title, crop_width=crop_width, requested_font_size=_OVERLAY_BASE_FONT
    )
    base_max_chars = max(12, min(22, int(crop_width / 36)))
    budget = int(base_max_chars * _OVERLAY_BASE_FONT / max(1, font_size))
    widest = max((len(line) for line in wrapped.split("\n")), default=0)
    if widest > budget:
        return (
            f"{words} words: overflows the 4-line overlay, worst line {widest} chars "
            f"against a {budget} budget ({widest / budget:.1f}x). It renders off-frame."
        )
    return None


BATCH_DELIMITER = "===== CLIP {index} ====="
_BATCH_HEADER_RE = re.compile(r"^=+\s*CLIP\s+(\d+)\s*=+\s*$", re.IGNORECASE)


def build_batch_prompt(
    evidences: list[ClipEvidence],
    *,
    variant: str,
    count: int = DEFAULT_CANDIDATE_COUNT,
    seed: int | None = None,
    example_count: int = DEFAULT_EXAMPLE_COUNT,
) -> str:
    """One prompt covering many clips - the shape production actually uses.

    The rules and the examples are stated ONCE and the clips follow, which is
    what ``draft_handoff.build_multi_account_batch_chat_prompt`` does and why a
    batch costs far less per reel than a single-clip prompt. It is also where
    single-clip results stop predicting anything: with one clip in front of it a
    model writes to that clip, while with eight it drifts toward one house
    sentence and reuses it, which is the failure this mode exists to expose.
    """
    if variant == VARIANT_CURRENT:
        raise ValueError("The control's own three-option matrix does not batch; run B/C/D.")
    if not evidences:
        raise ValueError("A batch needs at least one clip.")

    rules = _rules_for(variant)
    examples = _example_block(select_examples(count=example_count, seed=seed))

    clips: list[str] = []
    for index, evidence in enumerate(evidences, start=1):
        clips += ["", BATCH_DELIMITER.format(index=index), *evidence.context_lines()]

    return "\n".join(
        [
            f"Write on-screen title options for {len(evidences)} short-form video clips.",
            "Treat every clip separately. A line written for one clip must not "
            "describe another, and the clips do not share a subject.",
            "",
            "RULES (these apply to every clip below)",
            *rules,
            *examples,
            *clips,
            "",
            f"For EACH clip write {count} lines that are not rewrites of each other. "
            "Do not let the clips converge on one sentence shape: the point of a batch "
            "is that each clip gets the line it earns, not that they read as a series.",
            "",
            "RETURN FORMAT: for each clip, the delimiter line exactly as written above "
            f"({BATCH_DELIMITER.format(index='N')}), then that clip's lines, one per line, "
            "nothing else. No numbering, no headers, no recommended pick, no explanation.",
        ]
    )


def build_production_batch_prompt(
    evidences: list[ClipEvidence],
    *,
    variant: str,
    delimiters: list[str],
    caption_rules: list[str],
    caption_outro: str | None = None,
    option_count: int = 3,
    seed: int | None = None,
    example_count: int = DEFAULT_EXAMPLE_COUNT,
) -> str:
    """A batch prompt whose reply the shipping importer can read.

    Same title rules and evidence as :func:`build_batch_prompt`, but it asks for
    ONE title per reel under the headers ``draft_handoff.parse_pasted_draft``
    already parses, so the reply goes back through "Paste Draft From Clipboard"
    instead of being scored by hand.

    ``delimiters`` is passed in rather than built here because the real ones
    carry the account-relative ``(#id)`` the importer routes on, and deriving
    that needs the database. The caller builds them with the production
    function so the two can never disagree about the format.

    A caption is asked for even though captions are not what this experiment is
    about, because ``import_pasted_draft`` REJECTS the whole reel when an option
    carries a title and no caption ("the caption for Option 1 did not parse").
    A titles-only reply parses cleanly and then fails at the import guard one
    level up. ``caption_rules`` is therefore the production caption block passed
    straight through, unchanged and untested by this bench.
    """
    if variant == VARIANT_CURRENT:
        raise ValueError("The control's own three-option matrix does not batch; run B/C/D.")
    if len(delimiters) != len(evidences):
        raise ValueError(f"{len(evidences)} clips but {len(delimiters)} delimiters.")

    clips: list[str] = []
    for delimiter, evidence in zip(delimiters, evidences):
        clips += ["", delimiter, *evidence.context_lines()]

    return "\n".join(
        [
            f"Write on-screen titles for {len(evidences)} short-form video clips.",
            f"{option_count} options per clip. Treat every clip separately: a line "
            "written for one clip must not describe another, and the clips do not share "
            "a subject.",
            "",
            "ON-SCREEN TITLE RULES (these apply to every clip below)",
            *_rules_for(variant),
            *_example_block(select_examples(count=example_count, seed=seed)),
            "",
            "CAPTION RULES (the Instagram description, separate from the on-screen title)",
            *caption_rules,
            *(
                [
                    "- MANDATORY: the caption must include this exact line, alone on its "
                    f'own line, directly before the hashtag line: "{caption_outro}". Do '
                    "not reword it or merge it into a paragraph."
                ]
                if caption_outro
                else []
            ),
            *clips,
            "",
            # Each option leads with a DIFFERENT beat. Without this the three come
            # back as one hook at three word counts - measured across a real batch,
            # the Cupid clip returned "not one chair turned", "nobody turned their
            # chair" and "zero chairs, and CeeLo still asks", which is one option
            # wearing three coats and gives a reviewer nothing to choose between.
            f"- THE {option_count} OPTIONS MUST START FROM DIFFERENT BEATS, not the same "
            "beat reworded at different lengths. Pick the clip's beats first, then lead "
            "each option with a different one and let the rest follow. For the Cupid "
            "clip that is: one option leading on the song he chose, one leading on "
            "CeeLo's reaction, one leading on the empty chairs. Same story, three ways "
            "in. If two of your options could be swapped without changing what the "
            "viewer learns first, you have written one option twice.",
            "",
            "RETURN FORMAT. For each clip, repeat its header line EXACTLY as written "
            f"above, then the sections below it. {option_count} titles, then ONE caption "
            "that works under any of them.",
            "",
            delimiters[0],
            *[
                line
                for n in range(1, option_count + 1)
                for line in (f"Title Option {n}:", f"<title {n}, on its own line>")
            ],
            "Recommended Pick: Title Option N",
            "Shared Caption:",
            "<the caption>",
            "",
            "The headers 'Title Option 1:' and 'Shared Caption:' must be PLAIN TEXT. Not "
            "bold, not italic, no bullet, no numbering, nothing before them on the line. "
            "The importer matches them from the start of the line, so "
            "'**Title Option 1:**' silently discards that clip. Every reel needs both "
            "its titles and its caption: a reel with titles and no caption is rejected "
            "on import. Nothing else in the reply: no commentary, no summary, no notes.",
        ]
    )


def parse_batch_titles(reply: str) -> dict[int, list[str]]:
    """Split a batch reply into ``{clip index: candidate lines}``.

    A clip whose delimiter the model dropped is absent from the result rather
    than silently merged into its neighbour: a batch scored with clip 4's lines
    attributed to clip 3 is worse than a batch that reports clip 4 missing.
    """
    blocks: dict[int, list[str]] = {}
    current: int | None = None
    buffer: list[str] = []

    def flush() -> None:
        if current is not None:
            blocks[current] = parse_titles("\n".join(buffer))

    for raw in (reply or "").splitlines():
        header = _BATCH_HEADER_RE.match(raw.strip())
        if header:
            flush()
            current = int(header.group(1))
            buffer = []
            continue
        buffer.append(raw)
    flush()
    return blocks


# Anything the model prefixes or appends despite being asked for bare lines.
_NOISE_LINE = re.compile(
    r"^\s*(?:"
    r"here\b|these\b|sure\b|option\b|title\s*option\b|recommended\b|why\b|note[s]?\b"
    r"|niche\s*check\b|selection\s*notes\b|set\s*\d|rules?\b|candidates?\b"
    r")",
    re.IGNORECASE,
)
_LEADING_MARKER = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")
_WRAPPING_QUOTES = re.compile(r'^["“‘\']+|["”’\']+$')
_MIN_TITLE_WORDS = 3


def parse_titles(reply: str) -> list[str]:
    """Pull candidate lines out of a pasted model reply.

    Deliberately lenient about what it strips (numbering, bullets, wrapping
    quotes) and strict about what it drops: a preamble line or a stray
    "Recommended Pick" scored as a candidate would quietly corrupt the
    comparison, and a dropped line is visible in the count while a smuggled one
    is not.
    """
    titles: list[str] = []
    seen: set[str] = set()
    for raw in (reply or "").splitlines():
        line = _LEADING_MARKER.sub("", raw.strip())
        line = _WRAPPING_QUOTES.sub("", line).strip()
        if not line or line.endswith(":"):
            continue
        if _NOISE_LINE.match(line):
            continue
        if len(line.split()) < _MIN_TITLE_WORDS:
            continue
        key = line.casefold()
        if key in seen:
            continue
        seen.add(key)
        titles.append(line)
    return titles
