# Sidebar Rail Refresh Design

## Goal

Improve the desktop app sidebar so it stays compact, clearly shows the active page, and no longer looks clipped or crowded.

## Scope

In scope:

- compact left navigation rail in the main window
- active/inactive navigation button styling
- top rail branding treatment
- spacing and width adjustments needed to stop clipping and overlap
- targeted tests for sidebar layout and active-state behavior

Out of scope:

- new navigation destinations
- expandable sidebar with labels
- broader shell redesign
- account panel feature changes beyond layout safety

## Problems To Fix

The current rail has four concrete UI problems:

1. Navigation buttons appear visually cut off on the right edge because the rail and button sizing are too tight for the current border and padding.
2. The active page is hard to identify because inactive and active buttons are too similar in tone.
3. The top `NF` mark looks like another navigation button, which makes the hierarchy unclear and weakens the UI.
4. Lower rail content competes with navigation spacing, making the sidebar feel crowded and increasing the chance of overlap with adjacent panels.

## Recommended Approach

Keep the sidebar as a compact icon-only rail, but tighten the visual system so only real navigation items use button chrome.

This approach is preferred because it fixes the reported usability issues with the smallest behavior change. It preserves the MVP shell structure and avoids speculative work like a full navigation rewrite.

## Visual Design

### Rail Structure

- Keep the sidebar as a narrow icon rail.
- Increase the effective inner space enough to prevent right-edge clipping.
- Preserve the sidebar as a dedicated left column in the main body layout.

### Brand Treatment

- Remove the current boxed `NF` tile.
- Replace it with a small, non-interactive compact text label near the top of the rail.
- The brand mark must not use the same shape, hover treatment, border emphasis, or fill language as nav buttons.

### Navigation Buttons

- Keep the buttons icon-only.
- Slightly rebalance button width, icon size, and horizontal padding so the icon frame does not appear cropped.
- Use one consistent button shape for all actual nav items.

### Active State

- Add a clear left accent bar for the active page.
- Pair the accent bar with a subtle filled background so the selected item is obvious at a glance.
- Increase contrast between active and inactive icon states.
- Keep hover feedback lighter than the active state so the current page remains unambiguous.

### Spacing

- Add clearer separation between the brand mark, nav stack, and lower rail content.
- Ensure the lower sidebar control does not visually crowd the nav buttons.
- Keep vertical rhythm consistent so the rail reads as one clean column.

## Layout And Behavior

- The sidebar must remain compact and icon-only.
- The brand label is display-only and must not behave like a button.
- Navigation behavior should remain unchanged.
- If the account panel currently causes the central content or sidebar to overlap, fix that at the layout boundary rather than masking it with styling.
- Sidebar sizing should avoid clipping at normal desktop window sizes used by the current app.

## Implementation Notes

- Update the stylesheet rules for the sidebar rail, nav buttons, active state, and brand label.
- Adjust the sidebar widget composition so the brand element has its own visual identity.
- Review the row layout that contains the sidebar, main content, and account panel to ensure panel visibility changes do not cause component overlap.
- Keep changes localized to `src/nicheflow_studio/app/main_window.py` unless a small supporting refactor is needed for tests.

## Testing

Add or update focused UI tests that verify:

- the brand element is not a checkable navigation control
- the active page button exposes a distinct selected state
- sidebar toggle/account panel behavior does not regress
- compact sidebar behavior remains intact at the current window setup used by existing tests

Manual verification target:

- launch the app and confirm the active nav item is obvious
- confirm sidebar buttons are no longer visually clipped
- confirm opening the account panel does not cause visible overlap with the navigation rail

## Risks

- Pure stylesheet changes may hide, rather than fix, an underlying layout-width issue if the body row sizing is wrong.
- Making the rail too narrow while increasing contrast could keep the clipping problem visible.
- Reusing the old button object names for non-button branding would preserve the original hierarchy problem.

## Acceptance Criteria

The work is complete when all of the following are true:

1. The sidebar remains compact and icon-only.
2. The top brand mark no longer looks like a navigation button.
3. The active navigation item is immediately distinguishable through a left accent bar and stronger selected styling.
4. Sidebar items no longer appear cut off on the right edge.
5. Opening adjacent sidebar-related panels does not create visible overlap with navigation content in the normal app layout.
