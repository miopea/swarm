"""A default input field for the Queen, and a mobile landing that skips Command Center.

OPERATOR REQUEST 2026-08-18: "add the input field for the queen by default on desktop and
mobile … this will help me avoid my half typed commands getting passed with injections",
then "on mobile, hide the attention tab and start the queen in the full screen mode".

THE DEFECT IT REMOVES. The Ask Queen panel embedded her live PTY and offered only PRESET
buttons, so anything ad-hoc had to be typed straight into the terminal. A half-written
line sits in that input buffer, and an automated write — a relay, a nudge, a dispatch —
lands in the SAME buffer and is submitted along with it. The composer holds the draft in
the browser and posts the whole message in one server-side send, so there is no window in
which a partial line is exposed. It is the same reason `pty/bridge.py` is not the safe
path: that writes keystroke by keystroke, which is what typing IS.

WHY IT POSTS TO /api/workers/queen/send. That handler calls `send_to_worker` WITHOUT
`automated=True`, so it is the operator's own path — deliberately not deferred by the
#1451 selection-prompt guard, because the operator is the human a picker is waiting for.
Routing the composer through an automated path would make it worse than typing.

THESE ARE MARKUP AND CSS CLAIMS, so most of the acceptance is visual and belongs in a
390px browser. What IS testable is the defect class #1291 named: a class that is USED but
never DEFINED fails silently and looks like a layout bug — and a control that is rendered
but never wired looks exactly like one that works.
"""

from __future__ import annotations

from pathlib import Path

_WEB = Path(__file__).parent.parent / "src" / "swarm" / "web" / "templates"
_BASE = (_WEB / "base.html").read_text()
_DASH = (_WEB / "dashboard.html").read_text()
_JS = (
    Path(__file__).parent.parent / "src" / "swarm" / "web" / "static" / "dashboard.js"
).read_text()


# ---------------------------------------------------------------------------
# The composer exists, is wired end to end, and is visible by default
# ---------------------------------------------------------------------------


def test_the_queen_panel_has_a_free_text_composer():
    assert 'id="cc-queen-compose-input"' in _DASH
    assert 'data-action="ccQueenCompose"' in _DASH


def test_the_composer_is_inside_the_queen_panel_not_some_other_one():
    """Placement is the feature. A composer that renders under Attention would post to
    the Queen while looking like it belongs to something else."""
    body = _DASH.split("cc-ask-queen-body")[1].split("cc-panel cc-attention")[0]

    assert 'id="cc-queen-compose-input"' in body


def test_it_is_NOT_hidden_on_either_desktop_or_mobile():
    """THE OPERATOR'S ACTUAL WORDS: "by default on desktop and mobile". The existing
    worker composer (.mobile-send-bar) is gated behind `@media (pointer: coarse)` and so
    has never rendered on a desktop; this one must not repeat that."""
    block = _DASH.split('class="cc-queen-compose"')[1].split("</div>")[0]

    assert "hide-mobile" not in block
    assert "hide-desktop" not in block

    # And its CSS must not be scoped to a pointer type the way .mobile-send-bar is.
    coarse = (
        _BASE.split("@media (pointer: coarse)")[1] if "@media (pointer: coarse)" in _BASE else ""
    )
    assert ".cc-queen-compose {" not in coarse, (
        "the Queen composer is defined only under a coarse-pointer query — it will not "
        "render on desktop, which is half of what was asked for"
    )


def test_every_class_the_composer_uses_is_actually_defined():
    """#1291's defect class: a class USED but never DEFINED fails silently and reads as a
    layout bug rather than a missing rule."""
    for cls in ("cc-queen-compose",):
        assert f'class="{cls}"' in _DASH, f".{cls} is not used"
        assert f".{cls} " in _BASE or f".{cls}{{" in _BASE or f".{cls} {{" in _BASE, (
            f".{cls} is used in dashboard.html but defined nowhere in base.html"
        )


def test_the_handler_is_registered_so_the_button_does_something():
    """A `data-action` with no entry in the handler map renders a button that silently
    does nothing — indistinguishable from one that works until you press it."""
    assert "ccQueenCompose: ccQueenCompose," in _JS
    assert "function ccQueenCompose(" in _JS


def test_it_posts_to_the_operator_send_endpoint():
    body = _JS.split("function ccQueenCompose(")[1].split("function setupQueenComposer")[0]

    assert "/api/workers/queen/send" in body
    assert "message: msg" in body


def test_enter_sends_and_shift_enter_makes_a_newline():
    """Matches the worker composer's contract. A multi-line draft is the whole reason to
    compose outside the PTY, so Shift+Enter must not submit."""
    body = _JS.split("function setupQueenComposer(")[1].split("function ccQueenVerb")[0]

    assert "e.key === 'Enter' && !e.shiftKey" in body
    assert "ccQueenCompose()" in body


def test_the_composer_is_wired_at_init():
    """Defined but never called is the same silent failure one level up."""
    assert "setupQueenComposer();" in _JS


def test_a_failed_send_puts_the_text_back():
    """Clearing optimistically is right — leaving text after a success is how a Queen
    instruction gets sent twice. But losing a long draft to a transient 500 is worse than
    retyping, so the failure path must restore it."""
    body = _JS.split("function ccQueenCompose(")[1].split("function setupQueenComposer")[0]

    assert body.count("ta.value = msg;") >= 2, (
        "the draft is not restored on both the !r.ok and the .catch paths"
    )


def test_no_auto_submit_of_whatever_is_already_in_her_pty():
    """The composer must send only what the operator typed INTO IT. Reading the terminal
    buffer and submitting that is the exact hazard this replaces."""
    body = _JS.split("function ccQueenCompose(")[1].split("function setupQueenComposer")[0]

    assert "getElementById('cc-queen-compose-input')" in body
    for forbidden in ("term-holder", "\\r", "inlineTermWs"):
        assert forbidden not in body, f"the composer touches {forbidden!r} — it must post text only"


# ---------------------------------------------------------------------------
# Mobile: Attention hidden, Queen full screen on landing
# ---------------------------------------------------------------------------


_CC_MOBILE_MEDIA = "@media (max-width: 600px)"


def _in_mobile_media(rule: str) -> bool:
    """Is *rule* inside the Command Center's mobile media block?

    THE FIRST VERSION OF THIS HELPER ASSUMED `(pointer: coarse)` AND FAILED, WHICH IS HOW
    I FOUND THE REAL BUG. Command Center's mobile rules live in `@media (max-width: 600px)`;
    the touch-target rules are the coarse-pointer ones. My JS was checking coarse while my
    CSS was in the width block — they disagree on a narrow desktop window (Attention hidden,
    focus still 'attention') and on a large tablet (Queen landing, Attention still shown).
    The test failing is what surfaced it; both now use the width query.
    """
    i = _BASE.find(rule)
    if i < 0:
        return False
    at = _BASE[:i].rfind("@media")
    if at < 0:
        return False
    header = _BASE[at : _BASE.index("{", at)]
    return "max-width: 600px" in header


def test_attention_is_hidden_on_touch():
    rule = ".cc-attention { display: none; }"

    assert rule in _BASE, "the Attention panel still renders on mobile"
    assert _in_mobile_media(rule), (
        "Attention is hidden OUTSIDE the Command Center mobile query — that hides it on "
        "desktop too, which is not what was asked for"
    )


def test_the_now_single_option_focus_switcher_is_hidden_too():
    """A two-tab switcher with one tab left implies somewhere else to go."""
    rule = ".cc-mobile-focus { display: none; }"

    assert rule in _BASE
    assert _in_mobile_media(rule)


def test_the_helper_can_tell_the_two_apart():
    """POSITIVE + NEGATIVE CONTROL for _in_coarse_media, since the first version of it
    silently checked the wrong block. A desktop-only rule must NOT read as coarse."""
    assert _in_mobile_media(".cc-attention { display: none; }") is True
    assert _in_mobile_media(".cc-queen-compose { flex: 0 0 auto;") is False


def test_touch_defaults_the_stored_focus_to_the_queen():
    """A stored 'attention' choice from before this change would otherwise leave Command
    Center showing an empty column on a phone."""
    assert "_ccCoarse ? 'queen' : 'attention'" in _JS
    assert "ccMobileFocus(_ccCoarse ? 'queen' : _ccStored)" in _JS


def test_touch_lands_on_the_queen_full_screen():
    assert "ccQueenFullscreen();" in _JS
    landing = _JS.split("var restoredWorker = null;")[1].split("CC_HANDLERS")[0]

    assert "matchMedia(CC_MOBILE_QUERY)" in landing
    assert "ccQueenFullscreen()" in landing


def test_a_restored_worker_still_wins_over_the_mobile_landing():
    """A deliberate navigation must not be overridden. The restored-worker branch comes
    FIRST, so the coarse-pointer landing only applies when nothing was restored."""
    landing = _JS.split("var restoredWorker = null;")[1].split("CC_HANDLERS")[0]
    restored_at = landing.index("if (restoredWorker")
    coarse_at = landing.index("matchMedia(CC_MOBILE_QUERY)")

    assert restored_at < coarse_at, (
        "the mobile Queen landing is evaluated before the restored worker — a deliberate "
        "navigation would be thrown away on every phone load"
    )


def test_the_landing_falls_back_rather_than_leaving_a_blank_screen():
    landing = _JS.split("var restoredWorker = null;")[1].split("CC_HANDLERS")[0]
    branch = landing.split("matchMedia(CC_MOBILE_QUERY)")[1].split("} else {")[0]

    assert "show();" in branch, "if ccQueenFullscreen throws, the operator gets a blank landing"


def test_it_reuses_the_fullscreen_button_path_rather_than_a_second_route():
    """`ccQueenFullscreen` is what the ⛶ button calls. Landing via the same function
    means there is one definition of 'the Queen, full screen' to keep correct."""
    assert "function ccQueenFullscreen()" in _JS
    fn = _JS.split("function ccQueenFullscreen()")[1].split("}")[0]

    assert "_origSelectWorker('queen')" in _JS.split("function ccQueenFullscreen()")[1][:400]
    assert "hide()" in fn or "hide();" in _JS.split("function ccQueenFullscreen()")[1][:400]


# ---------------------------------------------------------------------------
# It has to be visible in BOTH themes, and look like it belongs
# ---------------------------------------------------------------------------


def _composer_css() -> str:
    i = _BASE.index(".cc-queen-compose {")
    return _BASE[i : _BASE.index(".cc-queen-actions {", i)]


def test_the_composer_uses_theme_tokens_not_hardcoded_colours():
    """THE BUG THE OPERATOR SCREENSHOTTED. The first version styled the field with
    rgba(255,255,255,0.06) background and rgba(255,255,255,0.14) border — dark-theme
    values. This fleet's light theme has --surface #FFFFFF and --border-default #C1B8AA,
    so white-on-white rendered as bare text floating on the panel: "the UI is ugly".

    A hardcoded colour is not wrong in the theme you happen to be looking at, which is
    exactly why it survives review."""
    css = _composer_css()

    assert "rgba(255,255,255" not in css.replace(" ", ""), (
        "the composer hardcodes white-alpha again — invisible on the light theme"
    )
    for prop in ("background:", "color:", "border:"):
        line = [ln for ln in css.splitlines() if prop in ln]
        assert line, f"the composer sets no {prop}"
    assert "var(--bg)" in css and "var(--text)" in css and "var(--border)" in css


def test_the_send_button_matches_the_action_row_beneath_it():
    """ "the button is funny" — it was `btn btn-sm btn-primary`. `.btn-primary` is not
    defined at all, so it fell through to the accent-filled base `.btn` while every
    sibling in the row below is `.btn-secondary`: a gold button among outlined ones, and
    smaller than all of them."""
    block = _DASH.split('class="cc-queen-compose"')[1].split("cc-queen-actions")[0]

    assert 'class="btn btn-secondary"' in block, "Send does not match its sibling buttons"
    assert "btn-sm" not in block, "Send is a size smaller than the row it sits above"
    assert "btn-primary" not in block, (
        ".btn-primary has no definition in base.html — it renders as the accent-filled "
        "base button, which is what made it the odd one out"
    )
