from __future__ import annotations

import pytest

from nicheflow_studio.db.models import UiSetting
from nicheflow_studio.db.session import get_session
from nicheflow_studio.services import ui_settings
from nicheflow_studio.services.ui_settings import UiSettingError


def test_unset_key_returns_the_default() -> None:
    """Never-set is a normal state, not an error."""
    assert ui_settings.get_setting("nicheflow.nothing.here") is None
    assert ui_settings.get_setting("nicheflow.nothing.here", "fallback") == "fallback"


def test_set_then_get_round_trips_each_value_shape() -> None:
    """One store has to hold every preference shape the UI needs: a niche
    string, an excluded-account id list, and a boolean toggle."""
    ui_settings.set_setting("batch.niche", "history")
    ui_settings.set_setting("batch.excludedAccounts", [4, 5, 8])
    ui_settings.set_setting("batch.autoDistribute", True)

    assert ui_settings.get_setting("batch.niche") == "history"
    assert ui_settings.get_setting("batch.excludedAccounts") == [4, 5, 8]
    assert ui_settings.get_setting("batch.autoDistribute") is True


def test_null_is_stored_and_read_back_as_null_not_missing() -> None:
    """"All niches" is null, and it has to survive as a real stored choice —
    otherwise picking it would fall back to whatever the default is."""
    ui_settings.set_setting("batch.niche", "history")
    ui_settings.set_setting("batch.niche", None)

    assert ui_settings.get_setting("batch.niche", "history") is None


def test_set_overwrites_rather_than_duplicating() -> None:
    for value in ("history", "movie", "history"):
        ui_settings.set_setting("batch.niche", value)

    with get_session() as session:
        rows = session.query(UiSetting).filter(UiSetting.key == "batch.niche").all()
    assert len(rows) == 1
    assert ui_settings.get_setting("batch.niche") == "history"


def test_value_survives_a_new_session() -> None:
    """The whole point: a preference outlives the process that wrote it.

    get_session() opens a fresh session, so reading through a different one than
    wrote it is the closest a unit test gets to 'close the app and reopen'.
    """
    ui_settings.set_setting("batch.excludedAccounts", [1, 2, 3])

    with get_session() as session:
        row = session.get(UiSetting, "batch.excludedAccounts")
        assert row is not None
        assert row.value == "[1, 2, 3]"

    assert ui_settings.get_setting("batch.excludedAccounts") == [1, 2, 3]


def test_get_settings_fetches_several_in_one_call() -> None:
    ui_settings.set_setting("batch.niche", "movie")
    ui_settings.set_setting("batch.autoDistribute", False)

    assert ui_settings.get_settings(
        ["batch.niche", "batch.autoDistribute", "batch.neverSet"]
    ) == {
        "batch.niche": "movie",
        "batch.autoDistribute": False,
        "batch.neverSet": None,
    }


@pytest.mark.parametrize("key", ["", "   "])
def test_blank_key_raises(key: str) -> None:
    with pytest.raises(UiSettingError):
        ui_settings.set_setting(key, "x")


def test_oversized_value_raises_rather_than_truncating() -> None:
    """Silently storing a truncated value would read back as corrupt JSON."""
    with pytest.raises(UiSettingError):
        ui_settings.set_setting("batch.huge", ["x" * 100] * 200)


def test_unserialisable_value_raises() -> None:
    with pytest.raises(UiSettingError):
        ui_settings.set_setting("batch.bad", {1, 2, 3})  # a set is not JSON


def test_corrupt_row_falls_back_to_the_default() -> None:
    """A preference is never worth breaking a screen over."""
    ui_settings.set_setting("batch.niche", "history")
    with get_session() as session:
        session.get(UiSetting, "batch.niche").value = "{not json"
        session.commit()

    assert ui_settings.get_setting("batch.niche", "movie") == "movie"
