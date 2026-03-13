# Development

## Prereqs

- Windows 10/11
- Python 3.11+

## Run

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python -m nicheflow_studio
```

## Project Layout (MVP)

- `src/nicheflow_studio/` — app code
- `data/` — local runtime data (ignored by git)
- `docs/` — documentation

## Notes

- If you hit download issues, `yt-dlp` can break when sites change; update it with:
  `.\.venv\Scripts\pip install -U yt-dlp`

