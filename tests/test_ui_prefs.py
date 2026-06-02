from nicheflow_studio.core import ui_prefs


def test_get_returns_default_when_no_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NICHEFLOW_DATA_DIR", str(tmp_path))

    assert ui_prefs.get_ui_pref("auto_publish_due_reels", False) is False
    assert ui_prefs.load_ui_prefs() == {}


def test_set_then_get_round_trips(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NICHEFLOW_DATA_DIR", str(tmp_path))

    ui_prefs.set_ui_pref("auto_publish_due_reels", True)

    assert ui_prefs.get_ui_pref("auto_publish_due_reels", False) is True
    assert (tmp_path / "ui_prefs.json").exists()


def test_set_preserves_other_keys(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NICHEFLOW_DATA_DIR", str(tmp_path))

    ui_prefs.set_ui_pref("alpha", 1)
    ui_prefs.set_ui_pref("beta", "two")

    prefs = ui_prefs.load_ui_prefs()
    assert prefs == {"alpha": 1, "beta": "two"}


def test_corrupt_file_falls_back_to_empty(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NICHEFLOW_DATA_DIR", str(tmp_path))
    (tmp_path / "ui_prefs.json").write_text("{ not json", encoding="utf-8")

    assert ui_prefs.load_ui_prefs() == {}
    assert ui_prefs.get_ui_pref("auto_publish_due_reels", False) is False
