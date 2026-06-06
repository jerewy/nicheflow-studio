# NicheFlow Studio Prompt

You are working on NicheFlow Studio, a Windows-first multi-account Instagram sourcing, processing, distribution, scheduling, and publishing application.

## Primary Objective

Preserve the working Python backend and operational Instagram workflow while incrementally replacing the PyQt6 UI with pywebview + React.

The first migration slice is Processing. It must support direct structured draft generation/revision, selection, export progress, scheduling, and publishing without requiring chat copy/paste.

## Current Priorities

1. Create the minimal pywebview + Vite/React/TypeScript shell.
2. Define a UI-independent background-job contract for long-running work.
3. Extract only the plain-Python Processing services needed by the first slice.
4. Package and smoke-test the Processing replacement before migrating another screen.

## Architecture Rules

- Keep Python, SQLite, FFmpeg, Playwright, Apify integrations, AI providers, pooling, scheduling, and publishing.
- Long-running work runs in background jobs and exposes structured progress.
- Bridge calls return quickly with small JSON payloads or job IDs.
- Keep the existing PyQt workflow until replacement parity is packaged and validated.
- Prefer narrow, reviewable extraction over broad backend refactoring.

## Scope Discipline

Do not expand into:

- backend rewrite or SQLite replacement
- Next.js, Electron, or a local HTTP API for the first desktop slice
- all-screen migration before Processing is validated
- TikTok, cloud sync, broad analytics, or speculative ML features

## What Good Output Looks Like

- narrow diffs
- runnable checkpoints
- preserved operational behavior
- evidence-based verification
- explicit packaging validation
