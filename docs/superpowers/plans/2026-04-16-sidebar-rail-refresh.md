# Sidebar Rail Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refresh the compact main-window sidebar so the active navigation item is clearly visible, the brand mark no longer looks like a button, and the rail no longer clips or visually overlaps adjacent content.

**Architecture:** Keep the fix localized to the existing `MainWindow` composition in `src/nicheflow_studio/app/main_window.py`. Solve the issue by separating brand styling from navigation styling, slightly rebalancing rail dimensions and spacing, and locking the layout behavior down with focused `pytest` UI tests in `tests/test_main_window.py`.

**Tech Stack:** Python 3, PyQt6, pytest

---

## File Structure

- Modify: `src/nicheflow_studio/app/main_window.py`
  - owns the sidebar stylesheet, rail widget composition, account panel layout, and current page selection behavior
- Modify: `tests/test_main_window.py`
  - owns the existing sidebar/account panel UI regression tests and should absorb the new visibility/layout assertions
- Create: `docs/superpowers/plans/2026-04-16-sidebar-rail-refresh.md`
  - this implementation plan

### Task 1: Lock In Sidebar UI Expectations With Tests

**Files:**
- Modify: `tests/test_main_window.py`
- Reference: `src/nicheflow_studio/app/main_window.py`

- [ ] **Step 1: Add a failing sidebar-brand test**

Insert a new test near `test_sidebar_toggle_and_compact_library_behavior` that verifies the brand is a passive label and not a nav button:

```python
def test_sidebar_brand_is_display_only(qt_app) -> None:
    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()

        assert isinstance(window._sidebar_brand, QLabel)
        assert window._sidebar_brand.objectName() == "sidebarBrand"
        assert window._sidebar_brand.text() == "NicheFlow"
        assert window._sidebar_brand.alignment() == (
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        assert window._sidebar_brand.minimumHeight() >= 16
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()
```

- [ ] **Step 2: Run the new brand test to verify it fails**

Run: `pytest tests/test_main_window.py::test_sidebar_brand_is_display_only -v`

Expected: FAIL because `window._sidebar_brand.objectName()` is currently `"brandMark"` and the text is currently `"NF"`.

- [ ] **Step 3: Add a failing selected-state and compact-width test**

Insert a second test that proves the compact rail still exists while the selected nav item exposes a distinct property we can style reliably:

```python
def test_sidebar_selected_state_and_compact_width(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.commit()

    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()

        window._set_current_page("downloads")
        qt_app.processEvents()

        assert window._sidebar_panel.width() >= 72
        assert window._sidebar_panel.width() <= 84
        assert window._module_buttons["downloads"].property("selected") is True
        assert window._module_buttons["scraping"].property("selected") is False
        assert window._sidebar_toggle_button.property("selected") is False
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()
```

- [ ] **Step 4: Run the selected-state test to verify it fails**

Run: `pytest tests/test_main_window.py::test_sidebar_selected_state_and_compact_width -v`

Expected: FAIL because the sidebar width is currently `64` and the nav buttons do not expose a `selected` property yet.

- [ ] **Step 5: Run the existing sidebar regression test as a control**

Run: `pytest tests/test_main_window.py::test_sidebar_toggle_and_compact_library_behavior -v`

Expected: PASS before implementation so we know the existing toggle behavior is covered before the layout changes.

- [ ] **Step 6: Commit the red test additions**

```bash
git add tests/test_main_window.py
git commit -m "test: lock sidebar rail refresh expectations"
```

### Task 2: Separate Brand Styling From Nav Styling And Rebalance The Rail

**Files:**
- Modify: `src/nicheflow_studio/app/main_window.py`
- Test: `tests/test_main_window.py`

- [ ] **Step 1: Update the sidebar stylesheet to support passive branding and a selected nav marker**

Replace the current sidebar-specific stylesheet block with a version that separates brand styling from button styling and adds a left accent marker via a dynamic property:

```python
QPushButton#sidebarToggle {
    background: #141d27;
    border: 1px solid #273244;
    color: #9fb2c8;
    padding: 0;
    min-height: 44px;
    min-width: 44px;
    max-height: 44px;
    max-width: 44px;
    border-radius: 14px;
    text-align: center;
}
QPushButton#sidebarToggle:hover {
    background: #1a2734;
    border-color: #35506d;
    color: #e6edf3;
}
QPushButton#sidebarToggle[selected="true"] {
    background: #1b2a3a;
    border-color: #4b88c7;
    color: #f4f8fc;
    border-left: 3px solid #76b7ff;
}
QPushButton#sidebarToggle:checked {
    background: #1b2a3a;
    border-color: #4b88c7;
    color: #f4f8fc;
    border-left: 3px solid #76b7ff;
}
QLabel#sidebarBrand {
    background: transparent;
    border: none;
    color: #8fa7c0;
    font-size: 8pt;
    font-weight: 700;
    letter-spacing: 0.08em;
    padding: 2px 2px 8px 2px;
}
```

- [ ] **Step 2: Change the brand widget from a boxed tile to a passive label**

Update the sidebar brand construction so it no longer resembles a button:

```python
self._sidebar_brand = QLabel("NicheFlow")
self._sidebar_brand.setObjectName("sidebarBrand")
self._sidebar_brand.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
self._sidebar_brand.setMinimumHeight(18)
```

- [ ] **Step 3: Widen the compact rail enough to stop clipping and rebalance its spacing**

Adjust the rail sizing and layout margins in the sidebar construction block:

```python
self._sidebar_panel = QFrame()
self._sidebar_panel.setObjectName("sidebar")
self._sidebar_panel.setFixedWidth(76)
sidebar_layout = QVBoxLayout()
sidebar_layout.setContentsMargins(12, 12, 12, 14)
sidebar_layout.setSpacing(12)
sidebar_layout.addWidget(self._sidebar_brand)
sidebar_layout.addWidget(self._sidebar_nav, stretch=1)
sidebar_layout.addWidget(self._sidebar_toggle_button, alignment=Qt.AlignmentFlag.AlignHCenter)
self._sidebar_panel.setLayout(sidebar_layout)
```

- [ ] **Step 4: Make selected state explicit instead of relying only on checkable appearance**

Add a helper and use it from `_set_current_page` so every nav button gets a stable `selected` property:

```python
def _sync_sidebar_selection(self) -> None:
    for page_name, button in self._module_buttons.items():
        is_selected = page_name == self._current_page
        button.setChecked(is_selected)
        button.setProperty("selected", is_selected)
        button.style().unpolish(button)
        button.style().polish(button)
```

Call it from `_set_current_page(...)` after `self._current_page` is updated:

```python
self._current_page = page_name
self._workspace_stack.setCurrentIndex(MODULE_PAGES.index(page_name))
self._sync_sidebar_selection()
self._sync_account_panel_visibility()
```

- [ ] **Step 5: Run the two new focused tests**

Run: `pytest tests/test_main_window.py::test_sidebar_brand_is_display_only tests/test_main_window.py::test_sidebar_selected_state_and_compact_width -v`

Expected: PASS

- [ ] **Step 6: Commit the sidebar styling and selection changes**

```bash
git add src/nicheflow_studio/app/main_window.py tests/test_main_window.py
git commit -m "feat: refresh compact sidebar rail styling"
```

### Task 3: Protect Against Layout Overlap When The Account Panel Opens

**Files:**
- Modify: `src/nicheflow_studio/app/main_window.py`
- Modify: `tests/test_main_window.py`

- [ ] **Step 1: Add a failing geometry regression test for panel overlap**

Insert a test that checks the sidebar, account panel, and main workspace do not occupy overlapping horizontal ranges when the account panel is shown:

```python
def test_account_panel_does_not_overlap_sidebar_or_workspace(qt_app) -> None:
    init_db()

    with get_session() as session:
        account = Account(name="YT Main", platform="youtube")
        session.add(account)
        session.flush()
        session.add(
            DownloadItem(
                source_url="https://youtube.com/watch?v=layout",
                title="Layout clip",
                status="downloaded",
                account_id=account.id,
            )
        )
        session.commit()

    window = MainWindow()
    try:
        window.resize(1280, 820)
        window.show()
        qt_app.processEvents()
        window._current_account_combo.setCurrentIndex(1)
        qt_app.processEvents()

        window._toggle_account_sidebar()
        qt_app.processEvents()

        sidebar_rect = window._sidebar_panel.geometry()
        account_rect = window._account_panel.geometry()
        workspace_rect = window._workspace_content.parentWidget().geometry()

        assert sidebar_rect.right() < account_rect.left()
        assert account_rect.right() < workspace_rect.left()
    finally:
        window._refresh_timer.stop()
        window._toast_timer.stop()
        window._hide_toast()
        window.close()
```

- [ ] **Step 2: Run the overlap test to see the current behavior**

Run: `pytest tests/test_main_window.py::test_account_panel_does_not_overlap_sidebar_or_workspace -v`

Expected: FAIL if the current layout really allows the visual overlap seen in the screenshot, or PASS if the clipping issue is purely in the rail sizing. Either result is useful: keep the test if it reproduces the bug, otherwise tighten it after manual inspection to assert the correct layout boundary.

- [ ] **Step 3: Normalize the body-row sizing so the rail, account panel, and workspace keep separate lanes**

If the new geometry test fails, update the body-row section to keep the lane boundaries explicit:

```python
workspace_panel = QWidget()
workspace_panel.setLayout(workspace_column)
workspace_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

self._account_panel.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
self._sidebar_panel.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

body_row = QHBoxLayout()
body_row.setSpacing(16)
body_row.addWidget(self._sidebar_panel, stretch=0)
body_row.addWidget(self._account_panel, stretch=0)
body_row.addWidget(workspace_panel, stretch=1)
```

If the test already passes, keep these size policies only if needed for clarity after the manual visual check.

- [ ] **Step 4: Re-run the targeted sidebar/account tests**

Run:

```bash
pytest tests/test_main_window.py::test_sidebar_toggle_and_compact_library_behavior `
       tests/test_main_window.py::test_sidebar_selected_state_and_compact_width `
       tests/test_main_window.py::test_account_panel_does_not_overlap_sidebar_or_workspace -v
```

Expected: PASS

- [ ] **Step 5: Commit the layout guard changes**

```bash
git add src/nicheflow_studio/app/main_window.py tests/test_main_window.py
git commit -m "fix: keep sidebar rail and account panel visually separated"
```

### Task 4: Final Verification And Manual UI Check

**Files:**
- Modify: none
- Verify: `src/nicheflow_studio/app/main_window.py`
- Verify: `tests/test_main_window.py`

- [ ] **Step 1: Run the full focused main-window regression slice**

Run:

```bash
pytest tests/test_main_window.py::test_workspace_is_blocked_without_current_account `
       tests/test_main_window.py::test_sidebar_brand_is_display_only `
       tests/test_main_window.py::test_sidebar_selected_state_and_compact_width `
       tests/test_main_window.py::test_sidebar_toggle_and_compact_library_behavior `
       tests/test_main_window.py::test_account_panel_does_not_overlap_sidebar_or_workspace `
       tests/test_main_window.py::test_accounts_page_forces_account_panel_visible -v
```

Expected: PASS

- [ ] **Step 2: Launch the app for a manual visual check**

Run: `python -m nicheflow_studio`

Manual checks:

- the top label reads as branding, not as a button
- the selected nav item is obvious without guessing
- icons no longer look cut off on the right edge
- opening and closing the account panel does not visually collide with the rail

- [ ] **Step 3: Capture the outcome in git status**

Run: `git status --short`

Expected: only the intended sidebar/test files remain modified before any final integration step.

- [ ] **Step 4: Commit the verification checkpoint if additional tweaks were needed during manual check**

```bash
git add src/nicheflow_studio/app/main_window.py tests/test_main_window.py
git commit -m "test: verify sidebar rail refresh"
```

If no further code changed after verification, skip this commit.
