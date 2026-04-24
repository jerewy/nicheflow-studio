# NicheFlow Studio Prompt

You are working on NicheFlow Studio, a Windows-first desktop MVP for building a local content library from YouTube and YouTube Shorts URLs.

## Primary Objective

Ship a small, reliable packaged Windows app that can:

- accept a supported URL
- queue and download it in the background
- store local metadata/history
- show clear status and error states
- support retry, remove, and open actions

## Current Priorities

1. Packaging the app for Windows
2. Verifying the packaged build works end to end
3. Improving URL validation and failure clarity
4. Keeping the UI code maintainable without unnecessary refactors

## Prioritization Rules

- Choose the smallest working solution first.
- Prefer reliability over feature count.
- Prefer explicit, testable behavior over clever abstractions.
- Match existing repo patterns unless there is a strong reason not to.
- Avoid adding dependencies unless clearly necessary.

## Scope Discipline

Do not expand into:

- new content platforms
- automation/upload flows
- analytics
- cloud sync
- ML/AI features

unless the current packaged YouTube MVP is already solid.

## What Good Output Looks Like

- narrow diffs
- working checkpoints
- clear explanation of tradeoffs when needed
- evidence-based claims about what was verified
