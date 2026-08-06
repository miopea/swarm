"""Mobile UI fixes from #1291, pinned where a test can actually judge them.

These are CSS/markup claims, so most of #1291's acceptance is visual and belongs in a
390px browser — the ticket says so explicitly. What IS testable is the class of defect
that caused three of the seven items: a utility class that is USED but never DEFINED,
which fails silently and looks like a layout bug.

That is how item 5 happened. `.text-center` appeared 8 times in config.html and was
defined nowhere, so the notification event-filter headers never centered — and the
apparent offset grew with the header word's length (Email ~15px, Webhook ~36px) because
header and checkbox both started at their grid column's left edge and only the header
was wide. Nothing in the suite could have caught that, so this file adds the check.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_WEB = Path(__file__).parent.parent / "src" / "swarm" / "web" / "templates"
_BASE = (_WEB / "base.html").read_text()
_CONFIG = (_WEB / "config.html").read_text()
_DASH = (_WEB / "dashboard.html").read_text()
_JS = (
    Path(__file__).parent.parent / "src" / "swarm" / "web" / "static" / "dashboard.js"
).read_text()


def _defined_classes() -> set[str]:
    """Class selectors defined in base.html's stylesheet."""
    return set(re.findall(r"\.([a-zA-Z][\w-]*)\s*(?:,|\{|:)", _BASE))


def test_the_class_scan_is_honest():
    """Positive control — a scan that found nothing would make the audit below pass
    vacuously, which is exactly the shape of the bug it is auditing."""
    defined = _defined_classes()
    assert "text-muted" in defined and "fw-bold" in defined, "class scan is broken"
    assert len(defined) > 100, f"only {len(defined)} classes found; scan is broken"


# --- item 5, and the class of defect behind it ----------------------------


def test_text_center_is_defined():
    """#1291 item 5's root cause. Used 8x, defined nowhere, so every use was a no-op."""
    assert ".text-center" in _BASE, "the .text-center utility is missing again"
    assert re.search(r"\.text-center\s*\{[^}]*text-align:\s*center", _BASE), (
        ".text-center exists but does not centre anything"
    )


@pytest.mark.parametrize("cls", ["text-center", "overflow-x-auto", "fw-bold", "text-muted"])
def test_utility_classes_used_by_config_are_defined(cls):
    """The generalisation. A used-but-undefined utility fails SILENTLY and presents as a
    layout bug, which cost this ticket one of its seven items."""
    if cls not in _CONFIG:
        pytest.skip(f".{cls} not used in config.html")
    assert cls in _defined_classes(), (
        f".{cls} is used in config.html but defined nowhere in base.html — it is a "
        f"silent no-op, the same defect as #1291 item 5"
    )


# --- item 2: Focus removed completely, not just hidden --------------------


def test_the_focus_button_and_all_its_code_are_gone():
    """Operator chose removal over making it configurable. A half-removal that left the
    CSS or the on-load restore would put users who had it enabled into a layout with no
    way back out, since the button that toggled it would be gone."""
    assert "btn-focus-mode" not in _DASH, "the Focus button is still in the markup"
    for token in ("toggleFocusMode", "exitFocusMode(", "initFocusMode"):
        assert token not in _JS, f"{token} survives in dashboard.js"
    assert "focus-mode" not in _BASE, ".detail-area.focus-mode CSS survives in base.html"


def test_removing_focus_left_the_stylesheet_balanced():
    """The focus-mode rules were NESTED inside a media query, so a careless cut would
    unbalance the whole stylesheet and silently break every rule after it."""
    styles = re.findall(r"<style[^>]*>(.*?)</style>", _BASE, re.S)
    assert styles, "no <style> block found — scan broken"
    for block in styles:
        assert block.count("{") == block.count("}"), "unbalanced braces in base.html CSS"


# --- items 3 and 4: mobile rules exist ------------------------------------


def test_config_inputs_go_full_width_on_mobile():
    """Item 3. A fixed 550px input inside a content-sized flex row is what pushed the
    page wider than the viewport, so content ended up clipped at the LEFT and
    unreachable (evidence 084423, 084512)."""
    assert re.search(
        r"@media[^{]*max-width:\s*768px[^}]*?\.config-input[^}]*width:\s*100%", _BASE, re.S
    ), "no mobile rule making .config-input full-width"


def test_config_values_are_left_aligned_on_mobile_only():
    """Item 4. Desktop right-alignment is deliberate (550px label-left/value-right row)
    and is NOT reversed; only the mobile case is scoped, and the rationale comment is
    rewritten rather than deleted."""
    assert re.search(
        r"@media[^{]*max-width:\s*768px[^}]*?\.config-input\s*\{\s*text-align:\s*left", _BASE, re.S
    ), "no mobile-only left-align rule for .config-input"
    assert re.search(r"text-align:\s*right", _BASE), (
        "the desktop right-align was removed, not scoped"
    )
    assert "ON PURPOSE for the desktop layout" in _BASE, (
        "the rationale for the desktop choice was deleted instead of rewritten"
    )


def test_the_dpad_has_a_light_mode_contrast_override():
    """Item 6, the half the operator added during the interview: the pad is a dark
    translucent panel with a green glyph, so in light mode the arrows are nearly
    invisible. The dark-theme appearance is unchanged."""
    assert 'data-theme="light"' in _BASE and "term-dpad-btn" in _BASE
    assert re.search(r'\[data-theme="light"\][^}]*\.term-dpad-btn', _BASE), (
        "no light-theme override for the d-pad buttons"
    )


# --- item 7: the task panel at 390px -------------------------------------


def test_task_rows_wrap_and_the_title_gets_its_own_line_on_mobile():
    """Item 7. The row is one flex line with ~10 children and only .task-title has
    flex:1, so the title was the only child that could shrink and the fixed ones ate
    the whole 390px — titles came out as one or two words per line. The fix lets the
    row wrap and gives the title a full-width line."""
    assert re.search(
        r"@media[^{]*max-width:\s*768px.*?\.task-item\s*>\s*\.flex-center\s*\{[^}]*flex-wrap:\s*wrap",
        _BASE,
        re.S,
    ), "no mobile rule letting the task row wrap"
    assert re.search(r"\.task-item\s+\.task-title\s*\{[^}]*flex:\s*1\s+1\s+100%", _BASE, re.S), (
        "the title does not get a full-width line on mobile"
    )


def test_the_filter_bar_still_scrolls_its_own_container():
    """PRIOR ART, deliberately preserved. The filter chips and tab strip clip in the
    same screenshot, but that is a scroll affordance added because a tab was once
    rendered permanently off-screen with no way to reach it. #1291 read the clipping as
    a defect; overriding it would undo a documented fix, so it is left alone."""
    assert re.search(r"\.filter-bar\s*\{[^}]*overflow-x:\s*auto", _BASE, re.S), (
        "the filter bar's own-container scrolling was removed"
    )
    assert re.search(r"\.tab-group\s*\{[^}]*overflow-x:\s*auto", _BASE, re.S), (
        "the tab strip's own-container scrolling was removed"
    )


# --- item 1: the worker strip becomes a dropdown on mobile ----------------


_PARTIAL = (_WEB / "partials" / "worker_list.html").read_text()


def test_the_mobile_switcher_is_rendered_by_the_partial():
    """Item 1. Rendered INSIDE the partial on purpose: it re-renders on every
    workers_changed swap, so the dropdown stays in sync with worker state without a
    separate JS sync path that could drift."""
    assert "worker-switcher-select" in _PARTIAL, "no mobile switcher in the worker partial"
    assert "worker-switcher-active" in _PARTIAL, "the pinned active-worker chip is missing"


def test_waiting_workers_are_not_sorted_last():
    """The switcher orders states EXPLICITLY. sort(attribute='state') is alphabetical,
    which would put WAITING last — and WAITING is the state that needs the operator
    most, so burying it would defeat the point of the change."""
    # Strip Jinja comments first: the template's own comment EXPLAINS why the
    # alphabetical sort is not used, and an earlier version of this test matched that
    # explanation and failed. A scan that matches the prose describing a bug reports
    # the bug as present.
    code = re.sub(r"\{#.*?#\}", "", _PARTIAL, flags=re.S)
    assert "sort(attribute='state')" not in code, "alphabetical state sort would bury WAITING"
    order = re.search(r"_states\s*=\s*\[([^\]]*)\]", _PARTIAL)
    assert order, "no explicit state order found"
    states = [x.strip().strip("'\"") for x in order.group(1).split(",")]
    assert states.index("WAITING") < states.index("SLEEPING"), f"bad order: {states}"


def test_a_worker_in_an_unlisted_state_still_appears():
    """If a new WorkerState is added, an explicit order list would silently drop it —
    and the dropdown is the ONLY way to reach a worker on mobile, so a dropped worker
    is an unreachable worker."""
    assert "w.state not in _states" in _PARTIAL, "workers in unlisted states would vanish"


def test_the_switcher_change_handler_is_delegated():
    """A directly-bound listener would die on the first htmx swap of the worker list,
    which presents as an intermittently dead dropdown rather than a broken one."""
    assert "worker-switcher-select" in _JS
    m = re.search(
        r"document\.addEventListener\('change'[^)]*\)[^{]*\{[^}]*worker-switcher-select", _JS, re.S
    )
    assert m, "the switcher handler is not delegated on document"


def test_the_pills_and_their_scroll_fade_are_hidden_on_mobile_not_deleted():
    """Desktop still uses the pill list (it is also the drag-to-reorder surface), so
    they are hidden on mobile rather than removed. The ::after scroll fade goes with
    them — it exists only because the row scrolled (#543), and leaving it would paint a
    gradient over the new switcher."""
    assert re.search(
        r"\.worker-list\s*>\s*\.panel-body\s*>\s*\.worker-item\s*\{\s*display:\s*none", _BASE
    ), "the mobile pills are not hidden"
    assert re.search(r"\.worker-list::after\s*\{\s*content:\s*none", _BASE), (
        "the scroll fade still paints with no scroller under it"
    )
    assert ".worker-item {" in _BASE, "the pill styles were deleted; desktop needs them"


def _render_worker_partial(workers, selected="swarm"):
    """Render the real partial with PRODUCTION-SHAPED data (plain dicts, as
    ``web/app.py::_worker_dicts`` emits).

    NOT MagicMock, and that distinction cost me a near-miss worth recording: Jinja's
    ``selectattr('state','equalto',X)`` returns ZERO matches against MagicMock objects
    while working correctly on dicts and real objects. A mock-based render made the
    switcher look broken — every normal worker missing, only the unlisted-state
    fallback rendering — and I nearly "fixed" correct template code because of it.
    That is the mirror of #1281, where a MagicMock board made a genuinely broken fix
    look fine. Assert against the shape production actually passes.
    """
    import jinja2

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(_WEB)), autoescape=True)
    return env.get_template("partials/worker_list.html").render(
        workers=workers, selected_worker=selected, worker_tasks={}, queen=None
    )


def _w(name, state, provider="claude"):
    return {
        "name": name,
        "state": state,
        "provider": provider,
        "path": "/tmp",
        "revive_count": 0,
        "worktree_branch": "",
        "state_duration": "2m",
        "in_config": True,
        "needs_operator_input": state == "WAITING",
        "context_pct": 0.0,
        "state_icon": "",
        "tokens": 0,
    }


def test_the_switcher_renders_every_worker_in_the_intended_order():
    """The load-bearing render test. The dropdown is the ONLY way to reach a worker on
    mobile once the pills are hidden, so a worker missing from it is a worker that
    cannot be opened at all."""
    workers = [
        _w("swarm", "BUZZING"),
        _w("sculpt-studio", "WAITING"),
        _w("api", "SLEEPING"),
        _w("zz", "WEIRD_STATE"),
    ]
    html = _render_worker_partial(workers)
    opts = re.findall(r'<option value="([^"]+)"', html)

    assert sorted(opts) == sorted(["swarm", "sculpt-studio", "api", "zz"]), (
        f"a worker is missing from the switcher and is unreachable on mobile: {opts}"
    )
    assert opts[0] == "sculpt-studio", f"WAITING must come first, got {opts}"
    assert opts.index("api") > opts.index("swarm"), "SLEEPING sorted above BUZZING"
    assert 'value="swarm" selected' in html, "the current worker is not preselected"
    assert "worker-switcher-active" in html, "no pinned active chip"
    assert html.count('class="worker-item') >= 4, "desktop pills were lost"
