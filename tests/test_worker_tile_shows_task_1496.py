"""#1496 — worker tiles must show WHICH task, and whether it is started.

Before #1486, dispatch failed silently and almost nothing reached ACTIVE, so an
ACTIVE-only tile was blank for every worker: the operator could not tell "nothing
assigned" from "assigned and stuck". The tile now shows both, differentiated.
"""

from __future__ import annotations

import re
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from jinja2 import Environment, FileSystemLoader

from swarm.tasks.task import STATUS_LABEL, TaskStatus
from swarm.web.app import _worker_pending_counts, _worker_task_cards, _worker_task_titles

TPL = "src/swarm/web/templates"
BASE = Path("src/swarm/web/templates/base.html")


def _task(num: int, title: str, status: TaskStatus, worker: str | None, on_hold: bool = False):
    return types.SimpleNamespace(
        number=num, title=title, status=status, assigned_worker=worker, is_on_hold=on_hold
    )


def _daemon(*tasks) -> MagicMock:
    d = MagicMock()
    d.task_board.all_tasks = list(tasks)
    return d


# --------------------------------------------------------------------- data


def test_card_carries_number_title_and_status() -> None:
    d = _daemon(_task(1496, "Dashboard worker tiles show no task", TaskStatus.ACTIVE, "swarm"))
    assert _worker_task_cards(d) == {
        "swarm": {
            "number": 1496,
            "title": "Dashboard worker tiles show no task",
            "status": "active",
            "label": "In Progress",
        }
    }


def test_label_comes_from_the_canonical_status_vocabulary() -> None:
    """Operator, 2026-08-11: "it isn't queued, that isn't the status, right?"

    He is right. The first tile hand-rolled its own words in the template —
    "working"/"queued" — so a task read QUEUED on the tile while the board two
    panels over called the same row ASSIGNED. STATUS_LABEL is the declared single
    source of truth and is already coverage-tested against every TaskStatus.
    """
    d = _daemon(_task(7, "t", TaskStatus.ACTIVE, "swarm"))
    assert _worker_task_cards(d)["swarm"]["label"] == STATUS_LABEL[TaskStatus.ACTIVE]


def test_no_hand_rolled_status_vocabulary_in_the_template() -> None:
    """Guards the source, because the map test above cannot see the template.

    Re-introducing a ternary would satisfy every data-layer assertion here and
    still print the wrong word on screen — which is exactly how this shipped.
    """
    src = (Path(TPL) / "partials" / "worker_list.html").read_text()
    assert "'queued'" not in src and "'working'" not in src


def test_an_assigned_task_is_not_reported_as_the_current_task() -> None:
    """REVERSES #1496's widening. Operator, 2026-08-11: "assigned workers
    shouldn't show in the list, as assignment doesn't mean anything".

    #1496 put ASSIGNED tasks on the tile's top line reasoning that a blank tile
    hid "assigned and stuck". That traded a missing fact for a false one: the
    line answers "what is this worker doing", and nobody has claimed to have
    started an assigned task. It goes to the pending count instead.
    """
    d = _daemon(_task(1498, "claim hook", TaskStatus.ASSIGNED, "swarm"))
    assert _worker_task_cards(d) == {}, "assignment is not a claim about the present"
    assert _worker_pending_counts(d) == {"swarm": 1}, "but it must not vanish either"


def test_active_wins_when_a_worker_somehow_holds_both() -> None:
    """ACTIVE is the asserted one, so it beats the merely-queued sibling."""
    d = _daemon(
        _task(1, "queued", TaskStatus.ASSIGNED, "swarm"),
        _task(2, "started", TaskStatus.ACTIVE, "swarm"),
    )
    assert _worker_task_cards(d)["swarm"]["number"] == 2

    d2 = _daemon(
        _task(2, "started", TaskStatus.ACTIVE, "swarm"),
        _task(1, "queued", TaskStatus.ASSIGNED, "swarm"),
    )
    assert _worker_task_cards(d2)["swarm"]["number"] == 2, "order of all_tasks must not decide it"


@pytest.mark.parametrize("status", [TaskStatus.DONE, TaskStatus.BLOCKED, TaskStatus.BACKLOG])
def test_finished_or_parked_work_is_not_shown_as_current(status: TaskStatus) -> None:
    d = _daemon(_task(9, "not current", status, "swarm"))
    assert _worker_task_cards(d) == {}


def test_worker_with_no_task_is_absent_from_the_map() -> None:
    """The template renders the idle state; the data layer says nothing."""
    assert _worker_task_cards(_daemon()) == {}


def test_active_only_helper_is_unchanged() -> None:
    """_worker_task_titles keeps its ACTIVE-only guarantee (2026-08-06 ruling).

    Both surfaces now agree on ACTIVE-only, which is the point. If this ever
    starts returning assigned work, the ruling has been silently reversed.
    """
    d = _daemon(_task(1, "queued", TaskStatus.ASSIGNED, "swarm"))
    assert _worker_task_titles(d) == {}


# ---------------------------------------------------------------- pending


def test_pending_counts_every_assigned_task_not_just_one() -> None:
    """A count is the honest shape: naming one of three would pick arbitrarily."""
    d = _daemon(
        _task(1, "a", TaskStatus.ASSIGNED, "swarm"),
        _task(2, "b", TaskStatus.ASSIGNED, "swarm"),
        _task(3, "c", TaskStatus.ASSIGNED, "other"),
    )
    assert _worker_pending_counts(d) == {"swarm": 2, "other": 1}


def test_parked_work_is_not_pending_on_anyone() -> None:
    """Parked means nobody should pick it up, so counting it inflates the queue
    with work deliberately set down — an operator acting on that number would be
    chasing tasks that are not waiting on them."""
    d = _daemon(_task(1, "parked", TaskStatus.ASSIGNED, "swarm", on_hold=True))
    assert _worker_pending_counts(d) == {}


def test_an_active_task_is_not_also_counted_as_pending() -> None:
    """Otherwise the tile would say "In Progress #2" and "1 pending" for one task."""
    d = _daemon(_task(2, "started", TaskStatus.ACTIVE, "swarm"))
    assert _worker_pending_counts(d) == {}


@pytest.mark.parametrize("status", [TaskStatus.DONE, TaskStatus.BLOCKED, TaskStatus.BACKLOG])
def test_closed_or_blocked_work_is_not_pending(status: TaskStatus) -> None:
    assert _worker_pending_counts(_daemon(_task(9, "x", status, "swarm"))) == {}


# ------------------------------------------------------------------ render


_CARD_ACTIVE = {"swarm": {"number": 1, "title": "t", "status": "active", "label": "In Progress"}}
_CARD_ASSIGNED = {"swarm": {"number": 1, "title": "t", "status": "assigned", "label": "Assigned"}}


def _render(cards: dict, pending: dict | None = None) -> str:
    env = Environment(loader=FileSystemLoader(TPL), autoescape=True)
    tpl = env.get_template("partials/worker_list.html")
    w = types.SimpleNamespace(
        name="swarm",
        state="BUZZING",
        path="/tmp",
        provider="claude",
        in_config=True,
        revive_count=0,
        worktree_branch="",
        needs_operator_input=False,
        context_pct=0.0,
        exit_code=None,
        crash_tail="",
        state_duration="2m",
    )
    return tpl.render(
        workers=[w],
        selected_worker="swarm",
        worker_tasks={},
        worker_task_cards=cards,
        worker_pending=pending or {},
        queen=None,
    )


def test_pending_renders_as_a_count_and_never_as_a_task_row() -> None:
    """Operator, 2026-08-11: "maybe show 'Pending Assigned Tasks' or something
    useful". Useful, but not on the line that says what the worker is doing."""
    html = _render({}, {"swarm": 3})
    assert "3 pending assigned" in html
    assert "worker-task" not in html, "a pending count must not render a task row"


def test_a_worker_can_show_both_a_current_task_and_a_queue() -> None:
    html = _render(_CARD_ACTIVE, {"swarm": 2})
    assert "In Progress" in html and "2 pending assigned" in html


def test_pending_line_is_absent_at_zero() -> None:
    """Same ruling as the idle task row: absence is the signal, not a "0 pending"
    label repeated down a 16-worker sidebar."""
    html = _render({}, {})
    assert "pending" not in html.lower()


def test_tile_renders_number_and_title() -> None:
    html = _render(
        {
            "swarm": {
                "number": 1496,
                "title": "Worker tiles show no task",
                "status": "active",
                "label": "In Progress",
            }
        }
    )
    assert "#1496" in html
    assert "Worker tiles show no task" in html


def test_tile_distinguishes_working_from_merely_queued() -> None:
    """The distinction survives, but it moved: it is now row-vs-count, not
    chip-colour, because the two facts are different KINDS of fact."""
    working = _render(_CARD_ACTIVE)
    queued = _render({}, {"swarm": 1})

    assert "task-chip-active" in working and "In Progress" in working
    assert "task-chip-active" not in queued, "a queued task must not borrow the working chip"
    assert "1 pending assigned" in queued
    assert working != queued, "the two states must not render identically"


def test_the_template_still_honours_a_cards_own_status_class() -> None:
    """_CARD_ASSIGNED is unreachable via _worker_task_cards today, but the
    template must not hard-code "active" — a future status reaching the card
    should carry its own class rather than silently render as in-progress."""
    assigned = _render(_CARD_ASSIGNED)
    assert "task-chip-assigned" in assigned and "Assigned" in assigned


def test_idle_worker_renders_no_task_row_at_all() -> None:
    """Operator ruling 2026-08-11: no task means NO TEXT, not an "idle" row.

    The original #1496 tile printed "idle / no task assigned" whenever the card
    was missing. Across a 16-worker sidebar that painted the same sentence into
    every tile, and — worse — made a genuinely idle worker look identical to one
    whose card failed to build, which is exactly the confusion the feature was
    added to remove. Absence is the signal; the state pill already says RESTING.
    """
    html = _render({}, {})
    assert "no task assigned" not in html
    assert "task-chip-idle" not in html
    assert "worker-task" not in html, "an empty task row is still a row — render nothing"
    assert "worker-pending" not in html, "and neither is an empty pending line"
    assert "None" not in html, "a missing card must not leak a Python None into the tile"
    assert "Undefined" not in html


def test_status_is_not_conveyed_by_colour_alone() -> None:
    """A greyscale screenshot — which is how the operator reported this — must
    still distinguish the two states, so the chip carries a word."""
    active = _render(_CARD_ACTIVE)
    queued = _render({}, {"swarm": 1})
    assert "In Progress" in active and "In Progress" not in queued
    assert "pending assigned" in queued and "pending assigned" not in active


# ---------------------------------------------------------------- contrast


def _luminance(hex_colour: str) -> float:
    """WCAG 2.1 relative luminance."""
    h = hex_colour.lstrip("#")
    parts = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in parts]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(fg: str, bg: str) -> float:
    a, b = _luminance(fg), _luminance(bg)
    lo, hi = sorted((a, b))
    return (hi + 0.05) / (lo + 0.05)


def _light_token(name: str) -> str:
    """Resolve a CSS custom property from the [data-theme="light"] block."""
    css = BASE.read_text()
    block = css.split('[data-theme="light"] {', 1)[1].split("}", 1)[0]
    m = re.search(rf"{re.escape(name)}\s*:\s*(#[0-9A-Fa-f]{{6}})", block)
    assert m, f"{name} not found in the light theme block"
    return m.group(1)


def _light_chip_colour(chip: str) -> str:
    """The text colour that actually applies to ``chip`` in light mode.

    Takes the LAST matching rule, which is how the cascade resolves equal-ish
    specificity here, and falls back to the unscoped rule when no light-mode
    override exists. Deliberately not "assert an override is present": that
    would test the shape of my fix rather than the property the operator cares
    about, and would red on a different-but-correct fix (say, recolouring the
    base rule to something legible in both themes).
    """
    css = BASE.read_text()
    found: list[str] = []
    for m in re.finditer(rf"([^{{}}]*\.{re.escape(chip)}[^{{}}]*){{([^}}]*)}}", css):
        c = re.search(r"(?<!-)color\s*:\s*(#[0-9A-Fa-f]{6})", m.group(2))
        if c:
            found.append(c.group(1))
    assert found, f"no rule sets a literal text colour for .{chip}"
    return found[-1]


@pytest.mark.parametrize(
    ("chip", "token"),
    [("task-chip-assigned", "--accent"), ("task-chip-active", "--success")],
)
def test_task_chips_meet_wcag_aa_in_light_mode(chip: str, token: str) -> None:
    """Operator, 2026-08-11: "queued is hard to see". It measured 2.32:1.

    The chip rules set a near-black text colour against a background token that
    INVERTS between themes: --accent is #F1B83D in dark and #7A5000 in light,
    --success is #7FCB87 and #2F6F3E. Dark-on-bright is fine; the same
    declaration in light mode is dark-on-dark. Nothing caught it because the
    colours were only ever eyeballed in dark mode, and a source-scan test that
    merely asserted "a colour is set" would have passed on both.

    4.5:1 is the AA threshold for normal-size text. The chip is 0.62rem, far
    under the 18.66px/14pt-bold that would let the 3:1 large-text exception
    apply, so the stricter number is the right one.
    """
    ratio = _contrast(_light_chip_colour(chip), _light_token(token))
    assert ratio >= 4.5, f".{chip} is {ratio:.2f}:1 against {token} in light mode; AA needs 4.5:1"
