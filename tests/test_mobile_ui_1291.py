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
