"""Discover a project's workflow instead of assuming it (v2 phase 2).

THE FAILURE THIS REPLACES, from the operator's real Jira on 2026-08-07::

    no transition to 'Done' found for IS-10278 (available: ['Waiting for support'])

The status map was hardcoded and GLOBAL: ``done -> "Done"``. It worked for WWD and was
refused by all 11 IS tickets, whose workflow has no Done transition. Nothing could see
that until an export failed, and then it repeated every sync interval. A map typed once
is also the artefact that silently rots when a Jira admin edits a workflow.

TWO CHANGES: the map is discovered from the project's real vocabulary, and it is
PER PROJECT — workflows differ per project, so "what does done look like here" has no
global answer. That single global map is the whole reason IS failed while WWD
succeeded.

The proposal is deliberately separate from the confirmation: Done / Resolved / Closed
are rarely interchangeable, and a wrong automatic choice transitions someone's ticket
while reporting success.
"""

from __future__ import annotations

import pytest

from swarm.config.models import JiraConfig
from swarm.integrations.jira_workflow import (
    flatten_statuses,
    propose_status_map,
    terminal_status_names,
)


def _status(name: str, category: str) -> dict[str, str]:
    return {"name": name, "category": category}


# A conventional software project.
_WWD_LIKE = [
    _status("To Do", "new"),
    _status("In Progress", "indeterminate"),
    _status("Done", "done"),
]

# The shape that actually broke: a service-desk workflow with no "Done".
_IS_LIKE = [
    _status("Open", "new"),
    _status("Waiting for support", "indeterminate"),
    _status("Waiting for customer", "indeterminate"),
    _status("Resolved", "done"),
]


# --- flattening the API payload ----------------------------------------------


def test_flatten_dedupes_statuses_repeated_across_issue_types():
    """`/project/{key}/statuses` returns one entry PER ISSUE TYPE, each with its own
    copy of the same statuses. Callers want the project's vocabulary, not the
    per-type breakdown."""
    payload = [
        {
            "name": "Task",
            "statuses": [
                {"name": "To Do", "statusCategory": {"key": "new"}},
                {"name": "Done", "statusCategory": {"key": "done"}},
            ],
        },
        {
            "name": "Bug",
            "statuses": [
                {"name": "To Do", "statusCategory": {"key": "new"}},
                {"name": "Done", "statusCategory": {"key": "done"}},
            ],
        },
    ]
    flat = flatten_statuses(payload)
    assert [s["name"] for s in flat] == ["Done", "To Do"], f"not de-duplicated: {flat}"


def test_flatten_survives_a_malformed_payload():
    """Jira responses vary by deployment; a missing statusCategory must not explode
    the setup flow."""
    payload = [{"statuses": [{"name": "Odd"}, {"name": ""}, {}]}]
    flat = flatten_statuses(payload)
    assert [s["name"] for s in flat] == ["Odd"]
    assert flat[0]["category"] == ""


# --- the proposal ------------------------------------------------------------


def test_a_conventional_workflow_maps_as_expected():
    proposal = propose_status_map(_WWD_LIKE)
    assert proposal["done"] == "Done"
    assert proposal["active"] == "In Progress"
    assert proposal["assigned"] == "To Do"


def test_the_service_desk_workflow_maps_done_to_resolved_not_done():
    """THE CASE THAT FAILED. A hardcoded 'Done' is refused here; discovery finds the
    project's own terminal status instead."""
    proposal = propose_status_map(_IS_LIKE)
    assert proposal["done"] == "Resolved", (
        f"discovery did not find this project's terminal status: {proposal}"
    )
    assert proposal["done"] != "Done", "still proposing the status that was refused"


def test_the_category_beats_a_misleading_name():
    """A status literally called 'Done' that sits in the To Do category must NOT be
    chosen to mean finished. statusCategory is universal; names are arbitrary, and
    trusting the name is how an export marks a ticket done by moving it backwards."""
    workflow = [
        _status("Done", "new"),  # a column named Done that means 'not started'
        _status("Finished", "done"),
    ]
    proposal = propose_status_map(workflow)
    assert proposal["done"] == "Finished", (
        f"the name won over the category, so 'done' maps to a To Do status: {proposal}"
    )


def test_an_unmappable_status_is_omitted_rather_than_guessed():
    """An absent mapping means 'we do not know' and can be refused honestly. A wrong
    mapping transitions someone's ticket to the wrong state and looks successful."""
    workflow = [_status("Open", "new")]  # no in-progress, no terminal
    proposal = propose_status_map(workflow)
    assert "done" not in proposal, f"invented a terminal status: {proposal}"
    assert "active" not in proposal, f"invented an in-progress status: {proposal}"
    assert proposal.get("assigned") == "Open", "the mappable case should still map"


def test_a_single_candidate_is_used_without_a_hint_match():
    """One status in a category is not a guess, it is the only option."""
    workflow = [_status("Backlog", "new"), _status("Cooking", "indeterminate")]
    assert propose_status_map(workflow)["active"] == "Cooking"


def test_ambiguity_with_no_hint_match_is_left_unmapped():
    """Two equally plausible candidates and no hint: pick neither. Choosing
    arbitrarily writes to real tickets on a coin flip."""
    workflow = [_status("Alpha", "indeterminate"), _status("Beta", "indeterminate")]
    assert "active" not in propose_status_map(workflow)


def test_terminal_names_come_from_the_project():
    assert terminal_status_names(_IS_LIKE) == ["Resolved"]
    assert terminal_status_names(_WWD_LIKE) == ["Done"]


# --- per-project maps --------------------------------------------------------


def test_the_map_is_resolved_per_project():
    """The direct fix for IS failing while WWD worked."""
    cfg = JiraConfig(
        enabled=True,
        projects=["WWD", "IS"],
        project_status_maps={
            "WWD": {"done": "Done"},
            "IS": {"done": "Resolved"},
        },
    )
    assert cfg.status_map_for("WWD")["done"] == "Done"
    assert cfg.status_map_for("IS")["done"] == "Resolved", (
        "both projects resolve to the same map, which is exactly what broke"
    )


def test_a_project_without_a_confirmed_map_falls_back_to_the_global_one():
    """Upgrades must not break: a v1 install has a global map and no per-project maps,
    and refusing to export until every project is re-confirmed would break a working
    integration on upgrade."""
    cfg = JiraConfig(enabled=True, projects=["WWD"], status_map={"done": "Done"})
    assert cfg.status_map_for("WWD")["done"] == "Done"
    assert cfg.status_map_for("ANYTHING")["done"] == "Done"


def test_confirmation_is_tracked_separately_from_the_map():
    """Discovery proposes; nothing writes to real tickets on a proposal alone."""
    cfg = JiraConfig(enabled=True, project_status_maps={"IS": {"done": "Resolved"}})
    assert cfg.is_confirmed("IS") is False, "a discovered map must not count as confirmed"
    cfg.confirmed_projects.append("IS")
    assert cfg.is_confirmed("IS") is True


# --- the export path uses it -------------------------------------------------


@pytest.mark.asyncio
async def test_export_resolves_the_map_from_the_tickets_own_project():
    """Wiring, and the reason the two tests above are not sufficient on their own:
    a correct per-project map that the export path never consults changes nothing."""
    from pathlib import Path

    src = Path("src/swarm/integrations/jira.py").read_text()
    body = src[src.index("async def export_status") : src.index("async def export_status") + 1800]
    code = "\n".join(ln for ln in body.split("\n") if not ln.strip().startswith("#"))
    assert "status_map_for(" in code, (
        "export still reads the GLOBAL status map, so every project is treated as "
        "though it had the same workflow — the bug this phase exists to fix"
    )
    assert "task.jira_key.split" in code, (
        "the project is not derived from the ticket, so the per-project map cannot be selected"
    )
