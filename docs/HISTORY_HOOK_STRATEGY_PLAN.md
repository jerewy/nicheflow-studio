# NicheFlow Studio — History Hook Strategy and Generation Plan

## 1. Purpose

This document defines how NicheFlow should generate on-screen hooks for the History account network.

The goal is to produce hooks that:

- stop viewers from scrolling;
    
- are clear within one second;
    
- feel surprising, nostalgic, emotional, or worth learning about;
    
- fit the broad-history content pool;
    
- work across multiple history accounts without requiring strict sub-niches;
    
- stay grounded in what is visible or verified;
    
- avoid generic clickbait and invented historical claims.
    

This hook system is intended for the **History Pool** only.

```text
HISTORY_ACCEPTED
→ history-specific hook generator
→ history destination accounts only
```

Movie content must continue using a separate cinema hook system.

```text
MOVIE_ACCEPTED
→ movie/cinema hook generator
→ movie destination accounts only
```

---

## 2. Main Observation From Reference Accounts

The reference account does not rely only on very short hooks.

Its successful-looking hook style is usually:

```text
Specific subject + unusual action / emotional payoff / historical context
```

Examples of the structure:

```text
This house literally blew up in 2007 instead of being demolished

This judge surprises a 100-year-old WWII veteran with a heroic gesture

The reactions of a woman being a pilot in 1963

What was YouTube like 20 years ago before all the ads

This stunt cleared tracks from a moving train with no special effects

Nobody expected Levity to pack Circuit Grounds this deep
```

These hooks are often approximately **9–16 words**, and sometimes longer when the concept needs explanation.

The useful lesson is not to copy the exact wording. The useful lesson is:

```text
Do not only describe the footage.
Explain why the footage is worth watching.
```

Weak:

```text
A tent attached to a motor scooter
```

Stronger:

```text
People actually attached camping tents to scooters in the 1950s
```

The second version immediately gives the viewer a reason to keep watching.

---

## 3. Core Hook Decision

### Previous rule to avoid

Do not restrict history hooks too aggressively to only 7–11 words.

That works for minimal aesthetic titles, but it is too limiting for curiosity-based history footage. Some historical clips need one additional detail so the viewer understands why the moment is interesting.

### New history hook length rule

```text
Preferred length: 9–16 words
Acceptable range: 7–18 words
Maximum visual result: 2 on-screen lines
```

A title may be slightly longer when it becomes clearer and more engaging.

### Visual priority

For the Past Moments Black template:

```text
A clear two-line explanatory hook is better than
a vague short hook with empty space on both sides.
```

The title should fill the visual area naturally without becoming dense or difficult to read.

---

## 4. What Makes a History Hook Engaging

A history hook does not need to feel like a meme. It needs to create one strong viewer reaction.

## 4.1 Curiosity

The viewer thinks:

```text
Wait, that existed?
```

Example:

```text
People actually attached camping tents to scooters in the 1950s
```

## 4.2 Nostalgia

The viewer thinks:

```text
Life used to look completely different.
```

Example:

```text
This was what a family road trip looked like in 1958
```

## 4.3 Modern Comparison

The viewer thinks:

```text
We would never do this today.
```

Example:

```text
Before camper vans, people tried taking tents on scooters
```

## 4.4 Emotional Human Moment

The viewer thinks:

```text
That is genuinely moving.
```

Example:

```text
This veteran returned to the aircraft he flew decades earlier
```

## 4.5 Surprise or Absurdity

The viewer thinks:

```text
Why did anyone make this?
```

Example:

```text
This machine was designed to walk directly through snow
```

## 4.6 Comment Invitation

The viewer naturally wants to respond:

```text
Would you use this?
Why did this disappear?
My grandparents had one of these.
```

Example:

```text
This 1950s camping idea honestly makes more sense than expected
```

---

## 5. Recency and Relatability Rules

### Does every history hook need recency?

No.

History content does not depend mainly on being recent. Its strongest advantage is that it shows something old, unusual, forgotten, emotional, or visually surprising.

Do not force modern references into every hook.

### When modern comparison is useful

Use modern comparison only when it helps viewers instantly understand the contrast.

Good:

```text
Before camper vans, people tried traveling like this
```

```text
Before smartphones, tourists carried cameras like this
```

```text
This is what airport travel looked like before security lines
```

Weak or forced:

```text
This would break the internet today
```

```text
People back then were built different
```

### Does every hook need to be relatable?

Not in a meme-account way.

For history, relatability means connecting past life to a familiar activity:

```text
camping
school
shopping
commuting
traveling
working
celebrating
eating
driving
using technology
```

A strange historical object becomes more engaging when the viewer understands the ordinary need behind it.

Object-only:

```text
A scooter tent from the 1950s
```

Relatable historical framing:

```text
This was how people went camping by scooter in the 1950s
```

---

## 6. General Hook Formula

Every generated history hook should try to include:

```text
Visible subject + reason it is surprising or meaningful
```

### Strong structure

```text
[Specific person/object/activity] + [unexpected fact, outcome, contrast, or emotion]
```

### Examples

```text
This scooter carried its own camping tent in the 1950s

This family recorded their first television arriving at home

People once used machines like this to clean entire city streets

This veteran returned to the plane he flew during the war

This was everyday airport travel before modern security checks
```

### Avoid generic history language

Do not default to:

```text
A fascinating moment from history

This incredible invention changed everything

Nobody talks about this historical moment

The forgotten invention that shocked the world

You will not believe this was real
```

These phrases are vague, repetitive, and often unverifiable.

---

## 7. Hook Angle System

NicheFlow should not generate only one title. For every accepted history clip, generate multiple hook angles.

### Default output per history clip

```text
Option 1: Curiosity / surprising fact
Option 2: Everyday-life or nostalgia framing
Option 3: Modern comparison or emotional meaning
```

The user selects the strongest title before rendering.

### Example: Scooter Tent Footage

#### Curiosity Angle

```text
People actually attached camping tents to scooters in the 1950s
```

#### Everyday-Life Angle

```text
This was how people went scooter camping in the 1950s
```

#### Modern-Comparison Angle

```text
Before camper vans, people tried taking tents on scooters
```

These three hooks use the same source footage, but each gives the viewer a different reason to watch.

---

## 8. Hook Scenarios and Rules

## Scenario 1: Strange Invention or Unusual Object

### Use when

The clip shows an unusual machine, tool, attachment, household object, vehicle accessory, or invention.

### Goal

Make viewers think:

```text
Wait, people actually used this?
```

### Hook patterns

```text
People actually used [object] to [purpose] in [era]

This [object] let people [unexpected use] in [era]

The [object] people once used for [ordinary activity]

This was designed to [surprising function]
```

### Examples

```text
People actually attached camping tents to scooters in the 1950s

This machine let families wash clothes without electricity

The travel accessory people once attached directly to their cars

This device was designed to help people cross frozen roads
```

### Avoid

```text
The invention nobody remembers

This changed human history forever

The most unbelievable device ever made
```

Use disappearance or importance claims only when verified.

---

## Scenario 2: Everyday Life in the Past

### Use when

The footage shows ordinary people shopping, eating, commuting, going to school, walking through cities, using household items, or enjoying leisure time.

### Goal

Create nostalgia and comparison with modern life.

### Hook patterns

```text
This was a normal [activity] in the [decade]

What [ordinary activity] looked like in [year/decade]

How people [activity] before [modern change]

A regular day in [place/era] looked like this
```

### Examples

```text
This was a normal grocery trip in the 1950s

What a school morning looked like seventy years ago

How families spent summer before phones and streaming

A regular commute through New York looked like this in 1948
```

### Avoid

Do not claim that life was better, simpler, or happier unless the post is clearly commentary.

Bad:

```text
Life was so much better before technology
```

Better:

```text
What a summer afternoon looked like before smartphones
```

---

## Scenario 3: Historical Transportation

### Use when

The clip shows old trains, planes, cars, scooters, boats, public transport, unusual travel methods, or infrastructure.

### Goal

Use movement and modern comparison to make the footage feel alive.

### Hook patterns

```text
This was how people traveled before [modern alternative]

When [vehicle] came with [unexpected feature]

This [vehicle] was built to [surprising function]

What traveling through [place] looked like in [era]
```

### Examples

```text
When scooters came with their own camping tents

This train cleared its tracks while still moving

What flying commercially looked like before modern airports

This car was built with a kitchen in the back
```

### Avoid

Do not automatically call every old vehicle “revolutionary,” “rare,” or “lost.”

---

## Scenario 4: Old Technology

### Use when

The clip shows phones, computers, televisions, cameras, communication systems, appliances, early digital products, or obsolete devices.

### Goal

Trigger comparison between familiar modern technology and its older form.

### Hook patterns

```text
This was how people [modern activity] before [current technology]

What [technology] looked like before [modern version]

People once needed this to [normal digital action]

This machine did something your phone does instantly now
```

### Examples

```text
This was how people sent photos before smartphones

What home computers looked like before the internet

People once needed this machine to make a phone call

This device stored less than a single phone photo today
```

### Verification rule

Technical specifications must be verified before including exact numbers.

---

## Scenario 5: Historic City or Place Footage

### Use when

The clip shows streets, landmarks, neighborhoods, public spaces, buildings, airports, train stations, or changing landscapes.

### Goal

Make the viewer compare the familiar place with its earlier form.

### Hook patterns

```text
What [place] looked like in [year/era]

This street looked completely different in [year]

Before [major change], this was [place]

A walk through [place] in [era]
```

### Examples

```text
What Times Square looked like before the giant screens

A walk through London streets in the 1930s

This neighborhood looked completely different seventy years ago

Before modern airports, arrivals looked like this
```

### Avoid

Do not state that a place is destroyed, gone, or unrecognizable unless verified.

---

## Scenario 6: Emotional Historical Human Moment

### Use when

The clip shows reunions, veterans, elderly people, family memories, memorials, first experiences, rescues, ceremonies, or personal reactions.

### Goal

Lead with the person and the meaningful action, not vague emotion.

### Hook patterns

```text
This [person] returned to [meaningful place/object] after [time/context]

The moment [person] saw [meaningful subject] again

This [person] was surprised with [specific gesture]

After [time/event], they finally [emotional action]
```

### Examples

```text
This veteran returned to the aircraft he flew decades earlier

The moment she heard her husband’s wartime recording again

This judge surprised a 100-year-old veteran with one final salute

After fifty years, they finally returned to their childhood home
```

### Avoid

Do not exaggerate emotion with:

```text
This will make you cry

The most emotional moment in history
```

Let the specific event carry the feeling.

---

## Scenario 7: War, Military, Disaster, or Sensitive Historical Footage

### Use when

The footage involves war, destruction, disasters, injuries, deaths, displacement, protests, or traumatic historical material.

### Goal

Stay clear and respectful. Do not turn tragedy into cheap engagement.

### Hook patterns

```text
Footage from [event/place/year] shows [specific visible fact]

This was recorded during [verified event]

The moment [specific event] changed daily life in [place]

People documented this as [event] unfolded
```

### Examples

```text
Footage from postwar Berlin shows families rebuilding their streets

This was recorded during the evacuation of the city

People filmed the aftermath as the flood reached their homes
```

### Avoid

```text
You won’t believe what happened next

This tragedy is absolutely insane

The disaster everyone forgot
```

### Review requirement

Sensitive material should always require manual review before posting.

---

## Scenario 8: Old Advertisement or Product Demonstration

### Use when

The footage is a historical commercial, demonstration, sales reel, product launch, or consumer item from the past.

### Goal

Frame the old advertising or product itself as historical content.

### Hook patterns

```text
This is how [product] was advertised in [year/decade]

People were once sold [product] like this

This commercial promised [specific claim] in [era]

What advertising looked like before [modern shift]
```

### Examples

```text
This is how television sets were advertised in the 1950s

People were once sold family cars with commercials like this

This old ad promised an entire kitchen of the future

What fast food advertising looked like before social media
```

### Important distinction

Historical advertisements may be valid history content. Current sponsored/campaign posts scraped from source accounts should normally be flagged and excluded from the content pool.

---

## Scenario 9: Famous Person or Celebrity History

### Use when

The clip shows a known historical figure, actor, musician, athlete, politician, inventor, or public personality in an older moment.

### Goal

Use the recognizable person plus a specific event.

### Hook patterns

```text
The moment [person] [specific action/context]

This was [person] before [verified later achievement]

When [person] appeared in [unexpected old context]

The interview where [person] explained [specific topic]
```

### Examples

```text
The moment Muhammad Ali surprised an audience with this answer

This was Steve Jobs presenting before Apple became a household name

When a young actor appeared in this forgotten television advertisement
```

### Verification requirement

Identity, year, event, and quotation must be verified before generating a definitive hook.

If not verified, use neutral wording or reject the hook until confirmed.

---

## Scenario 10: Funny or Absurd Old Footage

### Use when

The footage is genuinely comedic, awkward, strange, clumsy, or visually absurd.

### Goal

Use light humor without turning the page into a low-quality meme account.

### Hook patterns

```text
This [thing] worked much less smoothly than advertised

People really tried solving [problem] like this

This old demonstration went wrong almost immediately

The idea sounded better before anyone tested it
```

### Examples

```text
This 1950s invention worked much less smoothly than advertised

People really tried crossing snow with a machine like this

This product demonstration went wrong almost immediately
```

### Avoid

Do not use fake failure or exaggerate if the clip is merely unusual.

---

## Scenario 11: Skill, Achievement, or Extraordinary Precision

### Use when

The clip shows an archer, craftsman, athlete, pilot, worker, artist, performer, builder, or specialist doing something impressive.

### Goal

Name the skill and make the achievement understandable.

### Hook patterns

```text
This [person/role] managed to [specific achievement]

The precision it took to [specific action]

This was considered elite skill in [era/context]

Watch how [specific action] was done before [modern aid]
```

### Examples

```text
This Olympic archer splits an arrow with impossible precision

Watch how aircraft were guided before modern systems

This craftsman carved an entire pattern without electric tools
```

### Verification rule

Avoid record claims such as “greatest,” “first,” or “only” unless verified.

---

## Scenario 12: Science, Nature, or Educational Historical Footage

### Use when

The source contains experiments, early science films, natural phenomena, animal footage, medical demonstrations, or education films.

### Goal

Make the observation understandable without pretending every clip is historical trivia.

### Hook patterns

```text
This early film showed how [process] actually works

Scientists recorded [specific observation] like this in [era]

This demonstration explained [phenomenon] before digital animation

What researchers once used to study [subject]
```

### Examples

```text
This early film showed how plants breathe in real time

Scientists recorded whale movement like this before modern tracking

This classroom film explained electricity without animation
```

### Niche note

This content may fit the broad history network only when the archival or old-media aspect is strong. Modern science footage should not automatically enter a history pool.

---

## Scenario 13: Footage With Unknown Context

### Use when

The footage looks interesting, but the year, location, identity, product name, or historical story is unknown.

### Goal

Do not hallucinate context. Use only what is visible.

### Safe hook patterns

```text
This unusual machine was built for a very specific problem

People once traveled with equipment like this

This old footage captures a surprisingly clever design

A roadside stop looked very different with this setup
```

### Avoid

Do not invent:

```text
the 1950s
the first ever
rare footage
government project
failed invention
disappeared from catalogs
```

unless supported by the source or verified research.

### Software rule

If fact confidence is low, generated titles must avoid exact names, years, and historical claims.

---

## Scenario 14: Candidate Is Actually a Current Advertisement or Campaign

### Use when

The scraped source post contains paid partnership language, product promotion, discount codes, branded campaigns, or sales-focused content.

### Goal

Do not generate normal history hooks automatically.

### Workflow

```text
possible_ad = true
→ manual_review_required
→ normally ignore from History Pool
```

### Exception

A genuinely historical advertisement may remain usable if the ad itself is the archival subject.

Example:

```text
1957 refrigerator commercial
→ valid historical content

Current creator promoting a travel app
→ ignore as source campaign
```

---

## Scenario 15: Candidate Belongs to the Movie Niche Instead

### Use when

A scraped or manually imported video is a film scene, television clip, actor interview, animated scene, or movie-production content rather than history footage.

### Workflow

```text
wrong_niche_for_history = true
→ do not send to HISTORY_ACCEPTED
→ optionally move to MOVIE_CANDIDATES after manual approval
```

History hook generation must never run automatically on movie scenes.

---

## 9. Hook Structures To Implement

NicheFlow should rotate through these structures for history content.

## Pattern A: “This...” explanatory hook

Best for clear footage and broad reach.

```text
This [subject] [surprising action] in [era/context]
```

Examples:

```text
This scooter carried its own camping tent in the 1950s

This train cleared fallen branches while still moving

This machine helped families wash clothes without electricity
```

---

## Pattern B: “People actually...” curiosity hook

Best for strange objects, outdated practices, or surprising everyday behavior.

```text
People actually [did/used/attached/built] [surprising detail] in [era]
```

Examples:

```text
People actually attached camping tents to scooters in the 1950s

People actually watched television through screens this small

People once carried devices like this just to make calls
```

---

## Pattern C: “How people...” everyday-history hook

Best for normal routines from another era.

```text
How people [ordinary activity] in [era/before modern change]
```

Examples:

```text
How people went camping by scooter in the 1950s

How families traveled before camper vans became common

How office workers communicated before email
```

---

## Pattern D: “What ... looked like” visual-comparison hook

Best for places, routines, technology, and public life.

```text
What [familiar subject] looked like in [era/before change]
```

Examples:

```text
What airport travel looked like before modern security checks

What grocery shopping looked like seventy years ago

What New York traffic looked like in the 1930s
```

---

## Pattern E: “When...” nostalgia hook

Best for transport, products, places, and forgotten everyday possibilities.

```text
When [ordinary thing] [unexpected historical condition]
```

Examples:

```text
When scooters came with their own camping tents

When families recorded holidays on cameras this large

When train travel looked more like a hotel stay
```

---

## Pattern F: “The moment...” emotional human hook

Best for reactions, reunions, meaningful gestures, and major events.

```text
The moment [person] [specific emotional action]
```

Examples:

```text
The moment this veteran stepped inside his old aircraft again

The moment she recognized a song recorded decades earlier

The moment a town watched its old bridge disappear
```

---

## Pattern G: Question hook

Best for comment potential, but should be used selectively.

```text
Why did [object/practice] disappear?

Would people still use [object] today?

What happened to [verified old practice/product]?
```

Examples:

```text
Why did scooter tents disappear from road travel?

Would people still use a camping scooter today?

Why did homes stop using kitchens like this?
```

### Question rule

Question hooks should only be used when the caption or footage gives enough context for the viewer to respond meaningfully.

Do not overuse questions across the whole network.

---

## 10. Hook Style Distribution

Do not generate all history hooks in the same style.

For every accepted asset, generate three options:

```text
Hook Option 1: Direct curiosity / surprising fact
Hook Option 2: Nostalgia or daily-life framing
Hook Option 3: Comment-friendly comparison or question
```

### Example: Scooter Tent Footage

```text
Option 1:
People actually attached camping tents to scooters in the 1950s

Option 2:
This was how people went scooter camping in the 1950s

Option 3:
Would anyone still use a camping scooter today?
```

### Example: Old Airport Footage

```text
Option 1:
This was airport travel before modern security checks

Option 2:
What flying looked like when airports felt completely different

Option 3:
Would you rather travel through airports like this?
```

### Example: Old Household Appliance

```text
Option 1:
This machine once did the job of an entire kitchen appliance

Option 2:
How families handled laundry before modern washing machines

Option 3:
Would anyone still want this in their home today?
```

---

## 11. Engagement Quality Rules

## 11.1 Make the Hook Instantly Understandable

A viewer should understand the subject before the reel starts playing fully.

Good:

```text
People actually attached camping tents to scooters in the 1950s
```

Weak:

```text
The accessory that disappeared
```

The weak version hides too much context.

---

## 11.2 Use a Concrete Noun

Good hooks usually include an object, person, location, activity, or event.

Examples:

```text
scooter tent
train
washing machine
veteran
airport
family road trip
telephone
street market
```

Avoid hooks built only around abstract words:

```text
A moment history forgot

A fascinating time from the past

The idea that changed everything
```

---

## 11.3 Use One Clear Surprise

Do not overload a title with several facts.

Good:

```text
People actually attached camping tents to scooters in the 1950s
```

Too crowded:

```text
This rare 1950s postwar travel invention changed camping before disappearing forever
```

---

## 11.4 Mention the Year Only When Verified

Good when known:

```text
The reactions of passengers flying in 1963
```

Safe when unknown:

```text
The reactions of passengers flying decades before modern airports
```

Do not guess exact decades from the visual style alone.

---

## 11.5 Do Not Treat Exaggeration as Engagement

Allowed dramatic energy:

```text
People actually attached camping tents to scooters
```

Risky unsupported exaggeration:

```text
The scooter invention that almost changed transportation forever
```

The hook should be dramatic because the fact is interesting, not because the wording invents importance.

---

## 12. Reusing the Same Clip Across History Accounts

The same accepted history clip may eventually be reused across different history accounts.

When reusing, do not only change the caption. Change the **hook angle**.

### Example: Scooter Tent Footage

#### First Account — Curiosity

```text
People actually attached camping tents to scooters in the 1950s
```

#### Second Account — Nostalgia

```text
This was how people went scooter camping in the 1950s
```

#### Third Account — Modern Comparison

```text
Before camper vans, some travelers tried camping by scooter
```

#### Fourth Account — Comment Prompt

```text
Would you actually travel with a tent attached to a scooter?
```

### Reuse rule

```text
Same footage may reuse the same factual foundation,
but must use a new editorial angle when reposted later.
```

Do not randomize crop position merely to disguise reused footage. Keep visual quality consistent and vary the storytelling.

---

## 13. Past Moments Black Template Guidance

Past Moments Black should support clearer, slightly longer explanatory hooks than the initial short editorial design.

### Visual recommendation

```text
Preferred hook length: 9–16 words
Maximum display: 2 lines
Alignment: centered
Font: large and highly readable
Wrapping: natural enough to use the left/right space well
```

### Best fit for this template

```text
People actually attached camping tents to scooters in the 1950s
```

### Acceptable two-line feel

```text
People actually attached camping tents
to scooters in the 1950s
```

### Avoid titles that are too short and empty

```text
The Scooter Tent
```

### Avoid titles that become dense paragraphs

```text
People in the postwar era actually experimented with collapsible tents attached directly to motor scooters
```

---

## 14. Recommended Hook Generation Inputs

Before generating a hook, NicheFlow should gather:

```text
niche
visible_subject
visible_action
known_year_or_decade
known_location
known_person
verified_context
uncertainty_notes
sensitive_content_flag
possible_ad_flag
previous_hooks_for_same_asset
destination_account
```

### Example Input

```json
{
  "niche": "history",
  "visible_subject": "two riders using a dome-like shelter attached to a motor scooter",
  "visible_action": "the riders stop and sit under the scooter shelter",
  "known_year_or_decade": "late 1950s",
  "verified_context": "scooter camping accessory visible in source footage",
  "uncertainty_notes": "do not claim popularity or exact product name unless verified",
  "possible_ad_flag": false
}
```

---

## 15. Recommended Hook Generator Output

For every history asset, return:

```json
{
  "hook_options": [
    {
      "angle": "curiosity",
      "title": "People actually attached camping tents to scooters in the 1950s",
      "fact_risk": "low",
      "reason": "Names the unusual visible object and verified era directly."
    },
    {
      "angle": "nostalgia",
      "title": "This was how people went scooter camping in the 1950s",
      "fact_risk": "low",
      "reason": "Frames the footage as an older everyday travel lifestyle."
    },
    {
      "angle": "comparison",
      "title": "Before camper vans, some travelers tried camping by scooter",
      "fact_risk": "medium",
      "reason": "Creates modern comparison; only use if context supports it."
    }
  ],
  "recommended_title": "People actually attached camping tents to scooters in the 1950s",
  "recommended_angle": "curiosity"
}
```

---

## 16. Fact Confidence Rules

Every hook should have a fact-risk label.

## Low Risk

The hook only states what is visible or verified.

```text
This scooter carried its own camping shelter in the 1950s
```

## Medium Risk

The hook introduces a reasonable but not fully proven historical framing.

```text
Before camper vans, some travelers tried camping by scooter
```

## High Risk

The hook claims disappearance, rarity, invention, first occurrence, commercial failure, or historical impact without evidence.

```text
The forgotten invention that disappeared from every catalog
```

### Rule

```text
High-risk hook claims must not be automatically recommended.
They require source verification or manual rewrite.
```

---

## 17. Hook Scoring System

NicheFlow may rank generated hooks using a simple internal scoring model.

### Score criteria

|Criterion|Score|
|---|--:|
|Names a clear visible subject|+2|
|Contains a surprising but supported detail|+2|
|Understandable within one read|+2|
|Fits within two lines|+2|
|Includes verified era/year when useful|+1|
|Creates natural comment potential|+1|
|Uses vague clickbait language|-2|
|Makes unsupported historical claim|-4|
|Too generic for the specific footage|-2|
|Too long or visually crowded|-2|

### Example

```text
People actually attached camping tents to scooters in the 1950s
```

Expected score:

```text
Clear subject: +2
Supported surprise: +2
Easy to read: +2
Fits two lines: +2
Verified decade: +1
Comment potential: +1
Total: 10
```

---

## 18. Prompt Contract for Codex / Claude Implementation

Use this as the history hook generation instruction:

```text
Generate three on-screen Instagram Reel hook options for a broad history account.

The hook must be based only on the visible subject and verified context provided.
The account posts interesting historical footage, unusual old inventions, vintage everyday life, transportation, places, technology, and emotional archival moments.

Write hooks that make viewers curious about how people once lived, traveled, worked, invented, or reacted.

Rules:
- Preferred length: 9–16 words.
- Acceptable range: 7–18 words.
- Maximum visual result: two centered lines.
- Use simple, instantly readable language.
- Name the specific visible object, person, activity, or place.
- Include one clear surprise, contrast, emotional meaning, or historical context.
- Use a year or decade only when provided or verified.
- Do not invent rarity, disappearance, popularity, first-ever status, commercial failure, or historical importance.
- Do not use generic clickbait such as “you won’t believe,” “nobody talks about this,” “changed history forever,” or “the most unbelievable.”
- Do not use emoji or hashtags in the on-screen hook.
- If context is uncertain, write a safe hook based only on visible details.
- Produce three distinct angles:
  1. curiosity or surprising fact;
  2. nostalgia or everyday-life framing;
  3. modern comparison, emotional angle, or comment-friendly question.
- Recommend the strongest option and explain why briefly.
```

---

## 19. Example Outputs for Common History Assets

## Example A: Scooter Tent

```text
Curiosity:
People actually attached camping tents to scooters in the 1950s

Nostalgia:
This was how people went scooter camping in the 1950s

Comparison:
Would anyone still travel with a tent attached to a scooter?
```

Recommended:

```text
People actually attached camping tents to scooters in the 1950s
```

---

## Example B: Vintage Supermarket Footage

```text
Curiosity:
This was considered a modern grocery store in the 1950s

Nostalgia:
What grocery shopping looked like before supermarkets changed forever

Comparison:
Would you rather shop in a grocery store like this?
```

Recommended depends on footage and verified decade.

---

## Example C: Old Passenger Plane Interior

```text
Curiosity:
This was ordinary air travel before modern airport security

Nostalgia:
What flying looked like when plane travel still felt glamorous

Comparison:
Would you rather fly in an airplane cabin like this?
```

Use “glamorous” only when the visual footage clearly supports that tone.

---

## Example D: Wartime Street Recovery Footage

```text
Curiosity:
Families rebuilt these streets after the war ended

Human angle:
This footage shows daily life returning after years of destruction

Context:
People documented their city rebuilding one street at a time
```

Do not use playful or meme-style framing for sensitive footage.

---

## Example E: Old Technology Demonstration

```text
Curiosity:
People once needed a machine this large to store information

Nostalgia:
What using a computer looked like before personal laptops

Comparison:
Your phone now replaces everything this machine was built to do
```

The comparison hook requires verified context.

---

## 20. Implementation Priorities

## Version 1 — Must Build

```text
1. History-specific hook prompt profile.
2. Three hook angles per accepted history asset.
3. Preferred 9–16 word title rule.
4. Two-line visual fit check.
5. Fact-confidence labeling.
6. Reject or warn on unsupported high-risk claims.
7. Store all generated hook options per asset.
8. Store the selected final hook per assignment.
9. Allow a reused clip to receive a new hook angle.
10. Keep movie hook generation separate.
```

## Version 2 — Useful Later

```text
1. Automatic visual title-length preview.
2. Hook performance tracking by angle.
3. Recommend the best angle based on similar past posts.
4. Detect repeated hook wording across accounts.
5. Optional AI-generated topic tags.
```

---

## 21. Decisions Already Made

```text
Decision 1:
History hooks do not need to be limited to very short editorial phrases.

Decision 2:
Preferred history hook length is 9–16 words, with a maximum of two on-screen lines.

Decision 3:
The reference account style should be adapted through explanatory, curiosity-first wording, not copied literally.

Decision 4:
History hooks should prioritize clear visible subjects and interesting historical context.

Decision 5:
The hook should create curiosity, nostalgia, modern comparison, emotional response, or comment potential.

Decision 6:
Not every history hook needs recency or a relatable meme angle.

Decision 7:
Exact years, disappearance claims, rarity claims, and importance claims require verification.

Decision 8:
For reused source clips, vary the hook angle rather than randomly shifting crop positions.

Decision 9:
Past Moments Black should support slightly longer, more explanatory hooks with strong two-line readability.

Decision 10:
History hook generation must remain separate from movie/cinema hook generation.
```

---

## 22. Final Strategy

NicheFlow should generate history hooks using this principle:

```text
Do not merely label what is in the footage.
Tell the viewer why seeing it is surprising, human, or worth remembering.
```

The best history hooks are not the most dramatic ones. They are the ones that immediately show the viewer:

```text
This is an ordinary part of life that used to look unexpectedly different.
```

For the scooter tent footage, the recommended hook is:

```text
People actually attached camping tents to scooters in the 1950s
```

It is clear, surprising, visually supported, readable, and strongly aligned with the broad-history page strategy.

---

# Appendix A — Implementation Wording (FOR REVIEW · not yet applied)

Added 2026-05-31. This appendix is **not part of the original strategy**; it is the
exact prompt wording proposed for the codebase so it can be reviewed before any
edit lands. Nothing below has been written to `smart_drafts.py` yet.

## A.0 Why the current history hooks are weak (verified in code)

The strategy's diagnosis matches three concrete gaps in
`src/nicheflow_studio/processing/smart_drafts.py`:

1. **No history branch in `_niche_profile()`.** The "Past Moments Daily" account's
   `niche_label` is *"History moments, old clips, strange facts, and forgotten
   stories"*. The word **"stories"** accidentally matches the
   `podcast/interview/commentary/story` branch, so history footage is told to
   *"emphasize the sharpest idea, reveal, or quote-worthy takeaway"* — guidance
   written for talking-head clips, wrong for archival footage.
2. **No history branch in `_angle_plan()`.** History falls through to the generic
   *strongest-hook / curiosity / explanatory* plan, not the
   curiosity / nostalgia / modern-comparison angles this strategy wants.
3. **`history_lost_archive` title rules produce the banned anti-pattern.** Their
   current "good shapes" are *"The lost story behind this scene"*, *"Nobody
   expected this moment to matter"*, *"This old footage aged strangely"* at
   **5–11 words** — exactly the subject-hiding mystery-bait §11.1 says to avoid.

## A.1 Proposed `_niche_profile()` history branch

> **Implementation note:** this check must run BEFORE the `podcast/.../story`
> branch, and that branch must be guarded to NOT also fire when history keywords
> are present — otherwise the "forgotten stories" label triggers both and the
> guidance gets muddled.

```text
Write like a history page that makes ordinary past life feel worth watching.
NAME the visible subject (object, person, activity, vehicle, place, technology)
and pair it with the one reason it is surprising, nostalgic, emotionally human,
or different from today — do not merely label what is on screen. The on-screen
hook explains WHY the footage is worth watching, in plain, instantly readable
words. Ground every claim in what is visible or verified: never invent rarity,
disappearance, first-ever status, popularity, or historical importance, and use
an exact year or decade only when it is provided or verified. No meme framing,
no clickbait, no emoji or hashtags in the on-screen title.
```

## A.2 Proposed `_angle_plan()` history branch

> Place BEFORE the generic fallback `return`. The generator already produces and
> stores three options, so this only re-shapes what those three options are.

```text
Option 1 = curiosity / surprising fact: name the visible subject and the one
detail that makes a viewer think "wait, that existed?".
Option 2 = nostalgia / everyday-life framing: present the footage as how people
once lived, traveled, worked, shopped, or celebrated.
Option 3 = modern comparison, emotional human moment, or a comment-prompt
question — only when the footage or verified context supports it.
```

## A.3 Proposed `history_lost_archive` title-rule rewrite

Replaces the current bullets at `_caption_style_title_rules()` →
`if style == "history_lost_archive"`:

```text
- HARD RULE: each title NAMES the concrete visible subject (object, person,
  activity, vehicle, place, technology) and pairs it with ONE clear surprise,
  contrast, emotional meaning, or historical context. Preferred 9-16 words,
  acceptable 7-18, must fit two centered overlay lines. Explain WHY the footage
  is worth watching — never just label it.
- Rotate these shapes across the three options:
  'This [subject] [surprising action] in [era]',
  'People actually [did/used/built] [surprising detail] in [era]',
  'How people [ordinary activity] before [modern change]',
  'What [familiar subject] looked like in [era]',
  'When [ordinary thing] [unexpected condition]',
  'The moment [person] [specific emotional action]'.
- Good: 'People actually attached camping tents to scooters in the 1950s'.
  Weak (BANNED — hides the subject): 'The accessory that disappeared',
  'The lost story behind this scene', 'Nobody expected this to matter',
  'This old footage aged strangely'.
- FACT DISCIPLINE: name an exact year/decade only when provided or verified;
  otherwise use 'decades ago' / 'before modern [X]'. Never invent rarity,
  disappearance, first-ever status, popularity, commercial failure, or
  historical importance.
- BANNED: vague mystery bait ('the lost story', 'aged strangely', 'was not
  random'); clickbait ('you won't believe', 'shocking', 'changed history
  forever', 'nobody talks about this'); meme framing ('me when', 'POV:', 'bro',
  'send this to'); emoji and hashtags.
```

## A.4 Caveats to check after applying

- **Title fit:** 9–16 words is longer than the prior 5–11. The Past Moments Black
  template renderer already supports two-line / `\n\n` titles, but a real export
  must be eyeballed to confirm the longer hook wraps cleanly and isn't shrunk
  below readability by `_fit_title_band`.
- **Style routing:** confirm the history accounts actually select
  `history_lost_archive` (vs `narrative`) for the title. The `_niche_profile` /
  `_angle_plan` changes (A.1, A.2) apply regardless of style because they key off
  `niche_label`; the A.3 rewrite only bites when `history_lost_archive` is chosen.

## A.5 Deferred (not in this slice)

Per the §17 scoring system, §15 structured `hook_options` JSON with
`fact_risk`/`reason`, and §12 per-asset angle rotation for cross-account reuse —
the reuse piece is the same work as the sourcing/pooling plan's Phase 5 reuse
loop (`docs/SOURCING_POOLING_PLAN.md`), so it should land there, not here.
