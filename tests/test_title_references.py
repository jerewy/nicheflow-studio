from __future__ import annotations

import csv
from pathlib import Path

from nicheflow_studio.processing import title_references


def _write_reference_csv(root: Path, rows: list[tuple[str, int, float]]) -> None:
    target = root / title_references.REFERENCE_CSV_RELATIVE
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["on_screen_title", "word_count", "view_count", "engagement_rate"])
        for text, views, engagement in rows:
            writer.writerow([text, len(text.split()), views, engagement])


def _use_reference_root(monkeypatch, root: Path) -> None:
    monkeypatch.setattr(title_references, "data_dir", lambda: root)
    title_references.load_reference_titles.cache_clear()


def test_classify_register_buckets_by_voice_not_topic() -> None:
    assert title_references.classify_register("How is he this calm under pressure?") == "question"
    assert title_references.classify_register("I had never seen this before until now") == "first_person"
    assert title_references.classify_register("We grew up watching this every day") == "first_person"
    assert title_references.classify_register("Once buddies, always buddies") == "short_label"
    assert (
        title_references.classify_register(
            "The day a cockroach got into the room and this cat learned to run"
        )
        == "documentary"
    )


def test_load_reference_titles_drops_ocr_mis_segmentation(monkeypatch, tmp_path: Path) -> None:
    # The reference titles are OCR'd from screenshots, so a slice of them have
    # words glued together or stray single letters. These teach structure, and
    # broken spacing is exactly the structure we must not teach.
    _write_reference_csv(
        tmp_path,
        [
            ("A seal at the aquarium learned to mimic human speech", 900_000, 0.05),
            ("Thisishowtheworldsbestsoccerballs are produced", 800_000, 0.05),
            ("The capsule is at 2 2800 d degrees on reentry", 700_000, 0.05),
            ("If anyone wants to prank me like this I won'tbe mad", 600_000, 0.05),
            ("Someone recordedBats hanging upside down at night", 500_000, 0.05),
        ],
    )
    _use_reference_root(monkeypatch, tmp_path)

    texts = [entry.text for entry in title_references.load_reference_titles()]

    assert texts == ["A seal at the aquarium learned to mimic human speech"]


def test_load_reference_titles_returns_empty_without_the_csv(monkeypatch, tmp_path: Path) -> None:
    # The CSV lives under gitignored data/, so a fresh clone or a packaged build
    # will not have it. Callers must be able to fall back to static examples
    # rather than crashing mid-prompt.
    _use_reference_root(monkeypatch, tmp_path)

    assert title_references.load_reference_titles() == ()
    assert title_references.select_reference_titles() == []


def test_select_reference_titles_mixes_registers(monkeypatch, tmp_path: Path) -> None:
    # Documentary lines outnumber every other register roughly ten to one in the
    # real set, so a plain "top by views" pick would return documentary only.
    rows = [(f"Documentary line number {index} about a moment", 900_000, 0.05) for index in range(20)]
    rows += [
        ("How is he this calm under that kind of pressure?", 100_000, 0.05),
        ("Remember when this was on every single day?", 90_000, 0.05),
        ("I had never seen this one before today", 80_000, 0.05),
        ("We grew up with this and nobody talks about it", 70_000, 0.05),
        ("Once buddies, always buddies", 60_000, 0.05),
        ("What a time to be alive", 50_000, 0.05),
    ]
    _write_reference_csv(tmp_path, rows)
    _use_reference_root(monkeypatch, tmp_path)

    picked = title_references.select_reference_titles(count=8, seed=1)
    registers = {title_references.classify_register(text) for text in picked}

    assert len(picked) == 8
    assert len(set(picked)) == 8, "an example must never be repeated inside one prompt"
    assert registers == set(title_references.REGISTERS)


def test_select_reference_titles_rotates_between_generations(monkeypatch, tmp_path: Path) -> None:
    # A fixed example set is a fixed attractor: 50 of the last 360 generated
    # titles opened with "It looked like" while the prompt carried five
    # unchanging examples. Different seeds must produce different examples.
    rows = [(f"Documentary line number {index} about a moment", 900_000 - index, 0.05) for index in range(30)]
    _write_reference_csv(tmp_path, rows)
    _use_reference_root(monkeypatch, tmp_path)

    first = title_references.select_reference_titles(count=6, seed=1)
    second = title_references.select_reference_titles(count=6, seed=2)

    assert first != second
    assert title_references.select_reference_titles(count=6, seed=1) == first


def test_select_reference_titles_never_exceeds_the_requested_count(
    monkeypatch, tmp_path: Path
) -> None:
    # Round-robin refills from fat buckets when thin ones run dry; it must stop
    # at the requested count rather than draining a bucket.
    _write_reference_csv(
        tmp_path,
        [(f"Documentary line number {index} about a moment", 900_000, 0.05) for index in range(10)],
    )
    _use_reference_root(monkeypatch, tmp_path)

    assert len(title_references.select_reference_titles(count=3, seed=7)) == 3
