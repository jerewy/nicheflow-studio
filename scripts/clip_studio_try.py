"""Manual test harness for the Clip Studio service (before the UI exists).

Run with the project venv:

  # Fast, no download — cut a moment from a local video and render it:
  .venv\\Scripts\\python.exe scripts\\clip_studio_try.py render data\\clips\\avengers_doomsday_trailer.mp4 100 118 "The comeback nobody saw coming."
  # optional 6th arg = template (default historytrails_left):
  #   ... "Title" gaming_meme_black

  # Full pipeline — download a URL, transcribe it, and rank the moments
  # (downloads the whole source; best on a talky video like a documentary):
  .venv\\Scripts\\python.exe scripts\\clip_studio_try.py analyze https://youtu.be/HmY-G_DAHDI
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the package importable whether or not it's pip-installed in the venv.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from nicheflow_studio.services import clip_studio  # noqa: E402


def _render(args: list[str]) -> None:
    video, start, end, title = args[0], float(args[1]), float(args[2]), args[3]
    template = args[4] if len(args) > 4 else "historytrails_left"
    out = Path("data/clips/clip_studio_test.mp4")
    print(f"rendering {video}  {start:.0f}s–{end:.0f}s  template={template} …")
    result = clip_studio.render_clip(
        Path(video), out, start, end, title, template=template
    )
    print(f"done -> {result}")
    print("open it to check the title band + crop.")


def _analyze(args: list[str]) -> None:
    url = args[0]
    out = Path("data/clips/_analysis")
    print(f"downloading + transcribing {url} … (this can take a while for a long video)")
    result = clip_studio.analyze_url(url, out, top_n=10)
    print(
        f"\n{result['title']}  {result['width']}x{result['height']}  "
        f"{(result['duration_seconds'] or 0):.0f}s"
    )
    print(f"transcript: {'yes' if result['transcript_available'] else 'no usable English captions'}\n")
    for i, m in enumerate(result["moments"], 1):
        print(f"#{i}  {m['score']:>5}  {m['range_label']} ({m['duration']:.0f}s) [{m['length_note']}]")
        print(f"     why: {' | '.join(m['reasons'])}")
        print(f"     ctx: {m['context'][:130]}\n")
    if not result["moments"]:
        print("(no ranked moments — source has no usable transcript, e.g. a wordless trailer)")


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] not in {"render", "analyze"}:
        print(__doc__)
        return
    (_render if args[0] == "render" else _analyze)(args[1:])


if __name__ == "__main__":
    main()
