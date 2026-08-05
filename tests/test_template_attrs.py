"""Regression tests for HTML template attribute correctness.

Duplicate class= attributes on HTML elements cause the browser to ignore
all but the first, breaking styles (e.g. width: 120px instead of 100%).
"""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "src" / "swarm" / "web" / "templates"
STATIC_DIR = Path(__file__).resolve().parent.parent / "src" / "swarm" / "web" / "static"

# Matches opening HTML tags (possibly spanning multiple lines)
_TAG_RE = re.compile(r"<[a-zA-Z][^>]*>", re.DOTALL)


# Matches individual class="..." attributes within a tag
def _relative_luminance(hex_color: str) -> float:
    channels = [int(hex_color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(first: str, second: str) -> float:
    first_luminance = _relative_luminance(first)
    second_luminance = _relative_luminance(second)
    lighter = max(first_luminance, second_luminance)
    darker = min(first_luminance, second_luminance)
    return (lighter + 0.05) / (darker + 0.05)


def _theme_tokens(css: str, selector: str) -> dict[str, str]:
    match = re.search(rf"{re.escape(selector)}\s*\{{(?P<body>.*?)\}}", css, re.DOTALL)
    assert match, f"expected theme selector {selector}"
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9A-Fa-f]{6})", match.group("body")))


def test_modern_hive_themes_are_loaded_before_first_paint():
    """Theme choice must apply before the inline stylesheet paints the page."""
    base = (TEMPLATES_DIR / "base.html").read_text()
    login = (TEMPLATES_DIR / "login.html").read_text()
    theme_js = STATIC_DIR / "theme.js"

    assert theme_js.exists(), "expected one shared theme controller"
    for template in (base, login):
        script_index = template.find("/static/theme.js")
        style_index = template.find("<style>")
        assert 0 <= script_index < style_index


def test_modern_hive_palette_meets_wcag_contrast_floors():
    """Core text and component boundaries must meet WCAG 2.1 AA."""
    css = (TEMPLATES_DIR / "base.html").read_text()
    dark = _theme_tokens(css, ':root, [data-theme="dark"]')
    light = _theme_tokens(css, '[data-theme="light"]')

    for name, theme in (("dark", dark), ("light", light)):
        assert _contrast_ratio(theme["--text-primary"], theme["--canvas"]) >= 4.5, name
        assert _contrast_ratio(theme["--text-secondary"], theme["--surface"]) >= 4.5, name
        assert _contrast_ratio(theme["--border-strong"], theme["--surface"]) >= 3.0, name
        assert _contrast_ratio(theme["--on-accent"], theme["--accent-fill"]) >= 4.5, name


def test_theme_controller_supports_system_light_and_dark():
    js = (STATIC_DIR / "theme.js").read_text()
    for marker in (
        "swarm.theme",
        "prefers-color-scheme: dark",
        "data-theme",
        "themechange",
        "getTerminalTheme",
    ):
        assert marker in js
    assert "['system', 'light', 'dark']" in js


def test_theme_controls_are_accessible_on_dashboard_and_config():
    for filename in ("dashboard.html", "config.html"):
        template = (TEMPLATES_DIR / filename).read_text()
        assert 'data-action="cycleTheme"' in template
        assert 'class="theme-toggle' in template
        assert 'aria-label="Colour theme' in template


def test_terminal_theme_tracks_the_dashboard_theme():
    js = (STATIC_DIR / "dashboard.js").read_text()
    assert "window.swarmTheme.getTerminalTheme()" in js
    assert "window.addEventListener('swarm:themechange'" in js
    assert "entry.term.options.theme" in js


def test_light_terminal_ansi_palette_remains_readable():
    """Every ANSI foreground must remain legible on the light terminal surface."""
    js = (STATIC_DIR / "theme.js").read_text()
    light_match = re.search(r"light:\s*\{(?P<body>.*?)\n\s*\}", js, re.DOTALL)
    assert light_match
    colors = dict(
        re.findall(r"^\s*(\w+):\s*'(#[0-9A-Fa-f]{6})'", light_match.group("body"), re.MULTILINE)
    )
    background = colors["background"]
    foregrounds = {
        name: color for name, color in colors.items() if name not in {"background", "cursorAccent"}
    }

    assert foregrounds
    for name, color in foregrounds.items():
        assert _contrast_ratio(color, background) >= 4.5, name

    dashboard_js = (STATIC_DIR / "dashboard.js").read_text()
    assert (
        "minimumContrastRatio: window.swarmTheme.getTerminalMinimumContrastRatio()" in dashboard_js
    )
    assert "entry.term.options.minimumContrastRatio =" in dashboard_js


def test_sleeping_workers_remain_readable():
    css = (TEMPLATES_DIR / "base.html").read_text()
    assert '.worker-item[data-state="SLEEPING"] { opacity:' not in css


def test_light_worker_states_have_independent_visual_cues():
    """Light mode must show state without replacing the selected-worker cue."""
    css = (TEMPLATES_DIR / "base.html").read_text()

    for state in ("BUZZING", "WAITING", "RESTING", "SLEEPING", "STUNG"):
        assert f'[data-theme="light"] .worker-item[data-state="{state}"]' in css

    assert "inset-inline-end: 0" in css
    assert "width: 4px" in css
    assert ".worker-item:not(.selected) { background: var(--worker-state-tint); }" in css
    assert ".worker-meta > span:first-child" in css


def test_theme_reaches_login_offline_and_pwa_shell():
    login = (TEMPLATES_DIR / "login.html").read_text()
    offline = (STATIC_DIR / "offline.html").read_text()
    service_worker = (STATIC_DIR / "sw.js").read_text()
    pwa_route = (
        Path(__file__).resolve().parent.parent / "src" / "swarm" / "web" / "routes" / "pwa.py"
    ).read_text()

    assert "/static/theme.js" in login
    assert "/static/theme.js" in offline
    assert "'/static/theme.js'" in service_worker
    assert '"background_color": "#15130F"' in pwa_route
    assert '"theme_color": "#F1B83D"' in pwa_route


_CLASS_ATTR_RE = re.compile(r'\bclass\s*=\s*"[^"]*"')


def test_no_duplicate_class_attributes():
    """Every HTML tag should have at most one class= attribute."""
    errors: list[str] = []
    for template in sorted(TEMPLATES_DIR.glob("*.html")):
        content = template.read_text()
        lines = content.split("\n")
        # Track character offset → line number
        offset_to_line: list[int] = []
        for i, line in enumerate(lines, 1):
            offset_to_line.extend([i] * (len(line) + 1))  # +1 for \n
        for m in _TAG_RE.finditer(content):
            tag_text = m.group()
            class_matches = _CLASS_ATTR_RE.findall(tag_text)
            if len(class_matches) > 1:
                line_no = offset_to_line[m.start()] if m.start() < len(offset_to_line) else "?"
                errors.append(
                    f"{template.name}:{line_no} — tag has {len(class_matches)} "
                    f"class attributes: {class_matches}"
                )
    assert not errors, "Duplicate class= attributes found:\n" + "\n".join(errors)


def test_proposal_buttons_say_dismiss_not_reject():
    """Proposal reject buttons should be labelled 'Dismiss', not 'Reject'."""
    template = (TEMPLATES_DIR / "dashboard.html").read_text()
    js = (STATIC_DIR / "dashboard.js").read_text()
    # No reject-proposal button should have ">Reject<" label
    assert ">Reject<" not in template, "dashboard.html still has a >Reject< button label"
    assert ">Reject<" not in js, "dashboard.js still has a >Reject< button label"


def test_dashboard_has_paste_interception():
    """Ctrl-V paste must be intercepted so raw 0x16 doesn't reach Claude Code.

    The inline xterm.js terminal needs:
    1. attachCustomKeyEventHandler blocking Ctrl+V
    2. Capture-phase paste handler on the textarea
    Without these, Claude Code shows "No images found in clipboard" on paste.
    """
    content = (STATIC_DIR / "dashboard.js").read_text()
    # attachCustomKeyEventHandler must appear at least once (inline terminal)
    assert content.count("attachCustomKeyEventHandler") >= 1, (
        "dashboard.js must block Ctrl+V via attachCustomKeyEventHandler on the inline terminal"
    )
    # Capture-phase paste handlers (addEventListener('paste', ..., true))
    assert content.count("addEventListener('paste'") >= 2, (
        "dashboard.js must have capture-phase paste handlers"
    )


def test_question_mark_shortcut_skips_contenteditable():
    """The global ? help-shortcut handler must skip when the user is
    typing in a contenteditable element — the task editor's description
    field is a contenteditable div, and a missing isContentEditable
    guard swallows the ? keystroke and pops the shortcuts modal instead
    of letting the operator type a question mark.
    """
    js = (STATIC_DIR / "dashboard.js").read_text()
    marker = "? key opens keyboard shortcut help"
    i = js.find(marker)
    assert i >= 0, "expected the ? shortcut handler block in dashboard.js"
    # Inspect only the handler region (next ~900 chars after the comment).
    block = js[i : i + 900]
    assert "isContentEditable" in block, (
        "the ? shortcut handler must guard on isContentEditable so the "
        "task editor (contenteditable description) accepts a literal '?'"
    )


def test_queen_action_bar_reuses_worker_action_buttons():
    """The embedded Queen's quick-action bar must render from the SAME worker
    `action_buttons` config (advanced config tab), not a separate one, so the
    Queen matches the workers. Regression for the queen_action_buttons →
    action_buttons consolidation.
    """
    template = (TEMPLATES_DIR / "dashboard.html").read_text()
    i = template.find('class="cc-queen-actions"')
    assert i >= 0, "expected the cc-queen-actions block in dashboard.html"
    block = template[i : i + 1400]
    # Loops the shared worker config, not a separate queen config.
    assert "{% for btn in action_buttons %}" in block
    assert "queen_action_buttons" not in template, (
        "queen_action_buttons config was removed; the Queen reuses action_buttons"
    )
    # The circular "Ask Queen" action is skipped on the Queen herself.
    assert "btn.action != 'queen'" in block
    # Worker actions are routed to the Queen via the ccQueen* handlers.
    for marker in ("ccQueenVerb", "ccQueenRefresh", "ccQueenExport", "ccQueenSend"):
        assert marker in block, f"Queen action bar must map to {marker}"


def test_queen_export_handler_defined_and_registered():
    """ccQueenExport must exist and be wired into the CC click-delegation map,
    since the worker Export action maps to it for the Queen."""
    js = (STATIC_DIR / "dashboard.js").read_text()
    assert "function ccQueenExport(" in js, "ccQueenExport handler must be defined"
    assert "ccQueenExport: ccQueenExport" in js, "ccQueenExport must be in CC_HANDLERS"


# ---------------------------------------------------------------------------
# Cross-IIFE scope safety (dashboard.js)
# ---------------------------------------------------------------------------

# dashboard.js is split into two top-level IIFEs: the main dashboard (workers,
# terminals, tabs) and the Command Center (Queen embed, Attention queue, layout
# show()/hide()). They are SEPARATE scopes — a bare call from one to a function
# declared in the other throws ReferenceError at runtime. Because the CC's
# ``init()`` is a straight-line function, one such reference kills every
# statement after it: the Queen/Attention column splitter is never wired, the
# ``body.cc-active`` class is never applied (so the Command Center never
# renders and the task panel is left dangling under an empty "Select a worker"
# pane), and the digest/attention pollers never start.
#
# Regression: ``setupMobileComposer()`` was called bare from the CC ``init()``
# in 2026.6.8.2 while being declared in the main IIFE, dead-ending the whole
# Command Center. Cross-IIFE calls must go through ``window.``.

_FUNC_DECL_RE = re.compile(r"^\s*function\s+([A-Za-z_$][\w$]*)", re.MULTILINE)
_VAR_DECL_RE = re.compile(r"\b(?:var|let|const)\s+([A-Za-z_$][\w$]*)")
# Comments and string literals hold English prose ("the bottom grid row (and …)")
# that reads like a call site; blank them out before scanning for real calls.
_NOISE_RE = re.compile(
    r"/\*.*?\*/|//[^\n]*|'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|`(?:\\.|[^`\\])*`",
    re.DOTALL,
)


def _strip_js_noise(src: str) -> str:
    """Blank comments/strings, preserving newlines so line numbers stay true."""
    return _NOISE_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group()), src)


def _dashboard_iife_bodies() -> tuple[str, str]:
    """Split dashboard.js into (main IIFE body, Command Center IIFE body).

    Both IIFEs start at column 0 (``(function() {``) and end at column 0
    (``})();``), so the top-level boundaries are unambiguous by indentation.
    """
    lines = (STATIC_DIR / "dashboard.js").read_text().split("\n")
    bounds = [i for i, ln in enumerate(lines) if ln.startswith("})();")]
    assert len(bounds) == 2, (
        f"expected exactly 2 top-level IIFEs in dashboard.js, found {len(bounds)}"
    )
    return "\n".join(lines[: bounds[0]]), "\n".join(lines[bounds[0] + 1 : bounds[1]])


def test_dashboard_no_bare_cross_iife_calls():
    """No bare call from the Command Center IIFE into the main IIFE's scope."""
    main_body, cc_body = _dashboard_iife_bodies()
    cc_body = _strip_js_noise(cc_body)

    main_funcs = set(_FUNC_DECL_RE.findall(main_body))
    cc_local = set(_FUNC_DECL_RE.findall(cc_body)) | set(_VAR_DECL_RE.findall(cc_body))

    offenders: list[str] = []
    for name in sorted(main_funcs - cc_local):
        # A bare call is `name(` NOT preceded by `.` (which would be `window.name(`)
        # and not part of a longer identifier.
        m = re.search(r"(?<![.\w$])" + re.escape(name) + r"\s*\(", cc_body)
        if m:
            line_no = cc_body[: m.start()].count("\n") + 1
            offenders.append(f"{name}() @ CC-IIFE line {line_no}")

    assert not offenders, (
        "Command Center IIFE calls main-IIFE functions without `window.` — "
        "these throw ReferenceError and abort the rest of the enclosing "
        "function:\n  " + "\n  ".join(offenders)
    )


def test_dashboard_command_center_init_survives_missing_helpers():
    """CC ``init()`` must not lead with an unguarded cross-scope helper call.

    ``init()`` is the only thing that applies ``body.cc-active`` and wires
    ``attachCcResizeHandles()``. Anything it calls that lives in the other
    IIFE has to be reached defensively so a single missing helper can't take
    the whole Command Center down with it.
    """
    _, cc_body = _dashboard_iife_bodies()
    i = cc_body.find("function init() {")
    assert i >= 0, "expected the Command Center init() in dashboard.js"
    block = cc_body[i : cc_body.find("attachCcResizeHandles()", i)]
    assert "setupMobileComposer" in block, "init() should still wire the mobile composer"
    assert "window.setupMobileComposer" in block, (
        "init() must reach setupMobileComposer through `window.` — it is declared in the other IIFE"
    )


# ---------------------------------------------------------------------------
# Bottom task panel stays mounted behind a focused worker
# ---------------------------------------------------------------------------


def test_worker_view_keeps_task_panel_mounted():
    """Focusing a worker must collapse the task board, not unmount it.

    The operator shouldn't have to switch to the Queen dashboard just to read a
    task, so worker view keeps the bottom panel as a header-only strip (click a
    tab or the chevron to pop it open). Regression guard against the old
    ``bottom.style.display = 'none'`` + ``'1fr 0 0'`` treatment.
    """
    _, cc_body = _dashboard_iife_bodies()
    i = cc_body.find("function hide() {")
    assert i >= 0, "expected the Command Center hide() in dashboard.js"
    block = cc_body[i : cc_body.find("document.body.classList.remove('cc-active')", i)]
    assert "bottom.style.display = 'none'" not in block, (
        "worker view must keep the bottom task panel mounted (collapsed), not display:none"
    )
    assert "window.setBottomCollapsed" in block, (
        "hide() must route through setBottomCollapsed so the panel collapses to its header"
    )


def test_queen_view_restores_the_operators_collapse_preference():
    """The Queen view must HONOUR the stored minimize preference, not force
    the task panel open.

    It used to hardcode ``setBottomCollapsed(false, false)`` — "the Queen
    dashboard always shows the task board expanded" — so every return to the
    Queen silently discarded a minimize, and because expanding re-applies the
    saved split the panel also came back at its dragged size. Reported by the
    operator: "every time I go back to the queen it doesn't remember if the
    task list was minimized or how it was sized."

    persist=False still matters: restoring a preference must not rewrite it.
    """
    _, cc_body = _dashboard_iife_bodies()
    i = cc_body.find("function show() {")
    assert i >= 0, "expected the Command Center show() in dashboard.js"
    block = cc_body[i : cc_body.find("document.body.classList.add('cc-active')", i)]
    assert "window.setBottomCollapsed(false, false)" not in block, (
        "show() must not hardcode the task panel open — that discards a minimize"
    )
    assert "readBottomCollapsed" in block, (
        "show() must read the stored preference through the shared helper"
    )
    assert "window.setBottomCollapsed(queenCollapsed, false)" in block, (
        "restoring a preference must not persist it back"
    )


def test_all_collapse_readers_share_one_helper():
    """The Queen view and the worker view each read this preference their own
    way and disagreed — the worker honoured it, the Queen ignored it. One
    reader so they cannot drift apart again.

    It also returns None for "never set", because the worker view previously
    used ``storedCollapse !== ''`` and read an absent preference as
    "collapse", minimizing a panel nobody had asked to minimize.
    """
    js = (STATIC_DIR / "dashboard.js").read_text()
    assert "function readBottomCollapsed()" in js
    assert "window.readBottomCollapsed = readBottomCollapsed;" in js
    # Comment-stripped: the string survives in a comment explaining the fix,
    # and a raw `in js` check reads that as the defect still being present.
    # Third instance of comment-vs-code in this fleet today, and I hit it in
    # the test I wrote to guard against exactly this class of drift.
    live = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    live = re.sub(r"^\s*//.*$", "", live, flags=re.M)
    assert "storedCollapse !== ''" not in live, "absent preference must not mean collapsed"
    # Every getItem of the key must sit INSIDE the helper. Checked by SCOPE,
    # not per-line: a line-based check cannot tell that the helper's own
    # getItem lines are inside the helper — the same fixed-window mistake this
    # fleet has been correcting all week, which I reproduced writing this test.
    start = js.index("function readBottomCollapsed()")
    end = js.index("window.readBottomCollapsed = readBottomCollapsed;")
    outside = js[:start] + js[end:]
    assert "getItem('swarm_bottom_collapsed'" not in outside, (
        "a caller reads the collapse key directly instead of via readBottomCollapsed()"
    )


def test_collapse_preference_outlives_the_tab():
    """The panel's SIZE persists in localStorage ('swarm-split'). Keeping the
    collapsed flag in sessionStorage split one preference across two
    lifetimes: reopen the tab and the panel returned expanded, at the size you
    had dragged it to while minimized."""
    js = (STATIC_DIR / "dashboard.js").read_text()
    assert "localStorage.setItem('swarm_bottom_collapsed'" in js
    assert "sessionStorage.setItem('swarm_bottom_collapsed'" not in js


def test_bottom_collapse_helper_is_exported():
    """setBottomCollapsed lives in the main IIFE; the CC IIFE needs it on window."""
    js = (STATIC_DIR / "dashboard.js").read_text()
    assert "function setBottomCollapsed(collapsed, persist)" in js
    assert "window.setBottomCollapsed = setBottomCollapsed;" in js


def test_collapsed_bottom_panel_keeps_its_header_on_desktop():
    """Desktop collapse is header-only (the tab strip stays clickable), and the
    collapse chevron is no longer phone-only."""
    css = (TEMPLATES_DIR / "base.html").read_text()
    assert ".bottom-tabbed.collapsed > .tab-content.active { display: none; }" in css, (
        "collapsed desktop panel must hide the active tab body but keep the header"
    )
    assert (
        ".detail-area.bottom-collapsed {\n            grid-template-rows: 1fr auto auto;" in css
    ), "collapsed detail-area must reserve an auto row for the header strip"
    # display:none on the handle would drop it out of grid auto-placement and
    # slide the task panel into the wrong track (collapsing it to 0).
    i = css.find(".detail-area.bottom-collapsed > .resize-handle")
    assert i >= 0, "expected a collapsed-state rule for the resize handle"
    assert "display: none" not in css[i : i + 200], (
        "the collapsed resize handle must be hidden with visibility/height, not display:none"
    )
    assert ".btn-collapse { display: none; }" not in css, (
        "the collapse chevron must be visible on desktop now that the panel stays mounted"
    )


def test_split_drag_persists_the_applied_ratio():
    """endDrag must store the ratio moveDrag applied, not a fresh measurement.

    Re-measuring the detail-area at mouseup caught a mid-relayout height (xterm
    refits and the action bar reflow during the drag), so ``swarm-split`` ended
    up holding a ratio the operator never chose and the panel jumped on the next
    view switch or reload.
    """
    js = (STATIC_DIR / "dashboard.js").read_text()
    i = js.find("function endDrag() {")
    assert i >= 0, "expected endDrag() in the resizable-split IIFE"
    block = js[i : i + 900]
    assert "localStorage.setItem('swarm-split', lastRatio.toFixed(3))" in block, (
        "endDrag must persist the applied ratio (lastRatio)"
    )
    assert "area.children[0].getBoundingClientRect().height" not in block, (
        "endDrag must not re-measure the panel to derive the persisted split"
    )


def test_fullscreen_composer_is_restored_before_the_overlay_is_removed():
    """The fullscreen composer is a MOVED node, not a copy.

    ``openTerminalFullscreen`` docks the existing ``#mobile-send-bar`` into
    the overlay so the operator gets a real textarea (autocorrect,
    autocapitalize, dictation) instead of raw xterm keystrokes. Because the
    element is moved rather than cloned, tearing the overlay down while it is
    still docked would delete the detail panel's only touch input for the rest
    of the session — and it would look like an unrelated bug later, on a
    surface the operator never associates with fullscreen.

    So the close path must restore it FIRST. Asserting on order, not merely on
    presence: both calls existing in the wrong sequence is exactly the failure.
    """
    js = (STATIC_DIR / "dashboard.js").read_text()
    i = js.find("window.openTerminalFullscreen")
    assert i >= 0, "expected openTerminalFullscreen in dashboard.js"
    block = js[i : i + 6000]

    assert "function restoreComposer()" in block, "expected a restoreComposer helper"
    close_i = block.find("btn-close-fs').addEventListener")
    assert close_i >= 0, "expected the close handler"
    close_block = block[close_i:]

    restore_i = close_block.find("restoreComposer()")
    remove_i = close_block.find("overlay.remove()")
    assert restore_i >= 0, "close handler must restore the composer"
    assert remove_i >= 0, "close handler must remove the overlay"
    assert restore_i < remove_i, (
        "restoreComposer() must run BEFORE overlay.remove(), or the moved "
        "#mobile-send-bar is destroyed with the overlay"
    )


def test_fullscreen_composer_reuses_the_existing_send_bar():
    """No second input. A duplicate textarea would need its own send path,
    its own auto-grow wiring and its own autocorrect attributes, and would
    drift from the inline one."""
    js = (STATIC_DIR / "dashboard.js").read_text()
    i = js.find("window.openTerminalFullscreen")
    block = js[i : i + 6000]
    assert "getElementById('mobile-send-bar')" in block, (
        "fullscreen must dock the existing #mobile-send-bar"
    )
    assert "<textarea" not in block, (
        "fullscreen must not build its own textarea — reuse the composer"
    )


def test_fullscreen_composer_has_a_display_rule_outside_the_coarse_media_query():
    """``.mobile-send-bar.visible { display: flex }`` lives in
    ``@media (pointer: coarse)``, and Command Center sets ``display: none
    !important`` on the id. Docked inside the overlay the composer needs its
    own rule or it is present in the DOM and invisible."""
    css = (TEMPLATES_DIR / "base.html").read_text()
    assert ".terminal-fullscreen > #mobile-send-bar.visible" in css, (
        "expected an overlay-scoped display rule for the docked composer"
    )


def test_mobile_composer_has_no_inline_display_style():
    """An inline ``style="display:none"`` on #mobile-send-bar beat the rule
    that reveals it.

    ``selectWorker`` adds ``.visible``, and ``@media (pointer: coarse)`` has
    ``.mobile-send-bar.visible { display: flex }`` — but that rule carries no
    ``!important``, and an inline style attribute outranks any selector
    without one. Measured in Chromium at 414px with touch emulation: coarse
    pointer matched, the rule existed and matched, the class was applied, and
    computed display was still ``none``. So the touch composer — the only
    input path with autocorrect, autocapitalize and dictation — never
    rendered on any device.

    The base rule already hides it by default; the inline attribute was
    redundant as well as harmful.
    """
    html = (TEMPLATES_DIR / "dashboard.html").read_text()
    m = re.search(r'<div class="mobile-send-bar"[^>]*>', html)
    assert m, "expected the #mobile-send-bar element in dashboard.html"
    assert "style=" not in m.group(), (
        "#mobile-send-bar must not carry an inline style — it outranks the "
        f".visible rule that reveals it. Got: {m.group()}"
    )
    css = (TEMPLATES_DIR / "base.html").read_text()
    assert ".mobile-send-bar {" in css and "display: none;" in css, (
        "base.html must still hide the composer by default"
    )
