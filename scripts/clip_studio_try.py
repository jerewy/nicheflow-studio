"""Manual test harness for the Clip Studio service (before the UI exists).

Run with the project venv:

  # Fast, no download — cut a moment from a local video and render it:
  .venv\\Scripts\\python.exe scripts\\clip_studio_try.py render data\\clips\\avengers_doomsday_trailer.mp4 100 118 "The comeback nobody saw coming."
  # optional 6th arg = template (default historytrails_left):
  #   ... "Title" gaming_meme_black

  # Review pipeline — rank the moments, download the source once, and cut the
  # top candidates to short files you can actually watch before choosing:
  .venv\\Scripts\\python.exe scripts\\clip_studio_try.py previews https://youtu.be/HmY-G_DAHDI
  # optional 3rd arg = how many candidates (default 8)

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


def _previews(args: list[str]) -> None:
    url = args[0]
    count = int(args[1]) if len(args) > 1 else clip_studio.DEFAULT_PREVIEW_COUNT
    # One workspace per source so the cached download is reused across runs.
    workspace = Path("data/clips/_review") / _workspace_name(url)
    print(f"ranking + cutting {count} previews from {url} …")
    result = clip_studio.plan_and_preview(url, workspace, top_n=count)

    if not result["transcript_available"]:
        print("\nno usable English captions on this source, so nothing can be ranked.")
        print("cut it by hand instead:  clip_studio_try.py render <video> <start> <end> \"Title\"")
        return
    if not result["previews"]:
        print("\ntranscript found, but no moment cleared the length floor.")
        return

    source = result["source"]
    print(
        f"\nsource {source['width']}x{source['height']} "
        f"({'cached' if source['from_cache'] else 'downloaded now'}) -> {source['video_path']}"
    )
    print(f"previews in {workspace / 'previews'}\n")
    for preview in result["previews"]:
        activity = preview["visual_activity"]
        flag = "  [STATIC: probably one locked-off shot]" if activity["looks_static"] else ""
        print(
            f"#{preview['index'] + 1}  score {preview['score']:>5}  "
            f"{preview['range_label']} ({preview['duration']:.0f}s){flag}"
        )
        print(f"     why: {' | '.join(preview['reasons'])}")
        print(f"     ctx: {preview['context'][:130]}")
        print(f"     watch: {preview['video_path']}\n")
    print("watch them, then render the one you want:")
    print(
        f'  clip_studio_try.py render "{source["video_path"]}" <start> <end> "Your hook"'
    )


def _workspace_name(url: str) -> str:
    """A filesystem-safe folder name for one source URL."""
    safe = "".join(char if char.isalnum() else "_" for char in url)
    return safe[-60:].strip("_") or "source"


def main() -> None:
    args = sys.argv[1:]
    commands = {"render": _render, "analyze": _analyze, "previews": _previews}
    if not args or args[0] not in commands:
        print(__doc__)
        return
    commands[args[0]](args[1:])


if __name__ == "__main__":
    main()
