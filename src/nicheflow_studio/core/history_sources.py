from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HistorySourcePreset:
    handle: str
    note: str

    @property
    def source_url(self) -> str:
        return f"https://www.instagram.com/{self.handle}/"


# Starter source pool — the verified competitor/reference accounts (2026-06-01;
# see docs/competitor-learning-findings.md). Keep this list conservative and
# editable; it seeds source intake, it does not auto-download or auto-post.
# Four originally-proposed handles were dropped as invalid/nonexistent on the
# 2026-06-01 scrape (thelegendartist [personal art account], themysterist,
# thecinemast, entertainist, thelegendast); thelegendarist is the correct handle.
DEFAULT_HISTORY_SOURCE_PRESETS: tuple[HistorySourcePreset, ...] = (
    HistorySourcePreset("historytrails", "long-form documentary hook engine — the style we imitate"),
    HistorySourcePreset("theanomalists", "history anomalies and broad curiosity"),
    HistorySourcePreset("crazyfactscorner", "fact-led curiosity clips"),
    HistorySourcePreset("thehistologian", "high-volume history feed"),
    HistorySourcePreset("houseofhistorian", "history account reference"),
    HistorySourcePreset("factsontheway", "short fact/story hooks"),
    HistorySourcePreset("thelegendarist", "legend-style history/story clips"),
)


DEFAULT_HISTORY_SOURCE_URLS: tuple[str, ...] = tuple(
    preset.source_url for preset in DEFAULT_HISTORY_SOURCE_PRESETS
)
