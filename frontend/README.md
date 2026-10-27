# NicheFlow Studio UI

React + TypeScript + Vite + Tailwind front end for NicheFlow Studio. It runs inside
a pywebview window and talks to the Python backend through `window.pywebview.api`
(typed in `src/lib/bridge.ts`).

## Commands

Run these from this folder:

```powershell
npm install        # first time only
npm run dev        # Vite dev server on http://localhost:5173
npm run typecheck  # tsc -b (app and vite config)
npm run lint
npm run build      # writes dist/, which the desktop app loads
```

## Developing without the desktop shell

In a plain browser there is no pywebview bridge. `src/lib/bridge.ts` then falls
back to an in-memory mock, so every screen still renders with sample data.

To develop against the real backend with hot reload, start the dev server, then
launch the desktop window pointed at it:

```powershell
$env:NICHEFLOW_WEBVIEW_URL = "http://localhost:5173"
..\.venv\Scripts\python.exe -m nicheflow_studio.app.webview_app
```

`..\scripts\dev_webview.ps1 -Dev` does the same in one step.

## Layout

- `src/App.tsx`: tab shell (Accounts, Dashboard, Scraping, Processing, Clip Studio, Settings)
- `src/components/`: one file per screen, plus `ui/` primitives (shadcn-style)
- `src/lib/bridge.ts`: typed bridge wrapper and the browser mock
- `src/types.ts`: shapes returned by the Python bridge
