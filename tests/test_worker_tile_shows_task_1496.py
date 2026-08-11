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
from swarm.web.app import _worker_task_cards, _worker_task_titles

TPL = "src/swarm/web/templates"
BASE = Path("src/swarm/web/templates/base.html")


def _task(num: int, title: str, status: TaskStatus, worker: str | None):
    return types.SimpleNamespace(number=num, title=title, status=status, assigned_worker=worker)


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
    "working"/"queued" — so a BUZZING worker's ASSIGNED task read QUEUED, which
    names a task nobody has picked up: the opposite of what was on screen. The
    task board beside it said ASSIGNED for the same row, so two surfaces
    disagreed about one task. STATUS_LABEL is the declared single source of truth
    and is already coverage-tested against every TaskStatus member.
    """
    for status in (TaskStatus.ACTIVE, TaskStatus.ASSIGNED):
        d = _daemon(_task(7, "t", status, "swarm"))
        assert _worker_task_cards(d)["swarm"]["label"] == STATUS_LABEL[status]


def test_the_tile_never_says_queued_for_an_assigned_task() -> None:
    """The specific regression, asserted on the rendered output.

    Guards the word itself, not just the plumbing: re-introducing a ternary in
    the template would satisfy the map test above and still print "queued".
    """
    cards = _worker_task_cards(_daemon(_task(1501, "Choir rows", TaskStatus.ASSIGNED, "swarm")))
    html = _render(cards)
    assert "queued" not in html.lower(), "an ASSIGNED task is owned, not waiting in a queue"
    assert "Assigned" in html


def test_assigned_is_reported_as_assigned_not_hidden_and_not_promoted() -> None:
    """The whole point: visible, but NOT dressed up as in-progress.

    #1159 removed daemon-side activation inference after the promoter activated
    the wrong task. A worker that skipped swarm_start_task reads ASSIGNED here
    even while working — the honest answer, not a guess.
    """
    d = _daemon(_task(1498, "claim hook", TaskStatus.ASSIGNED, "swarm"))
    card = _worker_task_cards(d)["swarm"]
    assert card["status"] == "assigned", "an assigned task must not be reported as active"


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

    The new card function is additive. If this ever starts returning assigned
    work, the old ruling has been silently reversed.
    """
    d = _daemon(_task(1, "queued", TaskStatus.ASSIGNED, "swarm"))
    assert _worker_task_titles(d) == {}


# ------------------------------------------------------------------ render


_CARD_ACTIVE = {"swarm": {"number": 1, "title": "t", "status": "active", "label": "In Progress"}}
_CARD_ASSIGNED = {"swarm": {"number": 1, "title": "t", "status": "assigned", "label": "Assigned"}}


def _render(cards: dict) -> str:
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
        workers=[w], selected_worker="swarm", worker_tasks={}, worker_task_cards=cards, queen=None
    )


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


def test_tile_distinguishes_active_from_assigned() -> None:
    active = _render(_CARD_ACTIVE)
    assigned = _render(_CARD_ASSIGNED)

    assert "task-chip-active" in active and "In Progress" in active
    assert "task-chip-assigned" in assigned and "Assigned" in assigned
    assert active != assigned, "the two states must not render identically"


def test_idle_worker_renders_no_task_row_at_all() -> None:
    """Operator ruling 2026-08-11: no task means NO TEXT, not an "idle" row.

    The original #1496 tile printed "idle / no task assigned" whenever the card
    was missing. Across a 16-worker sidebar that painted the same sentence into
    every tile, and — worse — made a genuinely idle worker look identical to one
    whose card failed to build, which is exactly the confusion the feature was
    added to remove. Absence is the signal; the state pill already says RESTING.
    """
    html = _render({})
    assert "no task assigned" not in html
    assert "task-chip-idle" not in html
    assert "worker-task" not in html, "an empty task row is still a row — render nothing"
    assert "None" not in html, "a missing card must not leak a Python None into the tile"
    assert "Undefined" not in html


def test_status_is_not_conveyed_by_colour_alone() -> None:
    """A greyscale screenshot — which is how the operator reported this — must
    still distinguish the two states, so the chip carries a word."""
    active = _render(_CARD_ACTIVE)
    assigned = _render(_CARD_ASSIGNED)
    assert "In Progress" in active and "In Progress" not in assigned
    assert "Assigned" in assigned


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
