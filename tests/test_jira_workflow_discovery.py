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


# --- the operator's REAL IS workflow, which broke the first heuristic ---------

# Captured from a live discover against his instance, 2026-08-08. Kept verbatim
# because a synthetic workflow would not have contained "Reopened" next to "ToDo",
# and that pairing is what exposed the bug.
_IS_REAL = [
    _status("Canceled", "done"),
    _status("Done", "done"),
    _status("In Progress", "indeterminate"),
    _status("Reopened", "new"),
    _status("Resolved", "done"),
    _status("ToDo", "new"),
    _status("Waiting for customer", "indeterminate"),
    _status("Waiting for support", "indeterminate"),
    _status("Waiting On", "indeterminate"),
    _status("Work in progress", "indeterminate"),
]


def test_the_open_hint_does_not_match_reopened():
    """THE BUG THE OPERATOR'S SCREENSHOT EXPOSED. A plain substring search sent
    backlog, assigned AND unassigned to "Reopened", because the hint "open" appears
    inside "Re-open-ed". Whole-word matching rejects that."""
    proposal = propose_status_map(_IS_REAL)
    for swarm_status in ("backlog", "assigned", "unassigned"):
        assert proposal[swarm_status] != "Reopened", (
            f"{swarm_status} still maps to Reopened — 'open' is matching inside a "
            f"longer word, which puts new work into a reopened state"
        )


def test_todo_matches_the_to_do_hint_despite_the_missing_space():
    """The other half: the hint "to do" failed against a status literally named
    "ToDo" purely because of spacing, so the mapping fell through to a worse
    candidate."""
    proposal = propose_status_map(_IS_REAL)
    assert proposal["backlog"] == "ToDo", f"backlog did not find ToDo: {proposal}"
    assert proposal["unassigned"] == "ToDo"
    assert proposal["assigned"] == "ToDo"


def test_the_rest_of_the_real_workflow_maps_sensibly():
    proposal = propose_status_map(_IS_REAL)
    assert proposal["active"] == "In Progress"
    assert proposal["done"] == "Done"
    assert proposal["failed"] == "Canceled"
    # Three "Waiting" statuses are genuinely ambiguous; a whole-word hint match picks
    # one and the dropdown lets the operator correct it. What matters is that it
    # chooses a Waiting status rather than something unrelated.
    assert proposal["blocked"].lower().startswith("waiting")


def test_a_whole_word_hint_still_matches_inside_a_phrase():
    """Rejecting substrings must not break multi-word names: "waiting" has to keep
    matching "Waiting for customer", or the fix trades one wrong mapping for another."""
    assert propose_status_map(_IS_REAL)["blocked"] in (
        "Waiting for customer",
        "Waiting for support",
        "Waiting On",
    )


def test_whole_word_matching_is_load_bearing_on_its_own():
    """ISOLATES the whole-word rule from the normalisation rule.

    On the operator's real IS workflow both fixes point the same way — normalisation
    finds "ToDo" before whole-word matching is ever consulted — so a control that
    restored substring matching left every test green. That made the earlier cases
    evidence for normalisation only.

    Here there is NO exact match to short-circuit on: the project has "Reopened" and no
    To Do status at all. Substring matching sends new work to "Reopened" because "open"
    sits inside it; whole-word matching declines and leaves it unmapped, which the
    operator can then fix from the dropdown.
    """
    workflow = [
        _status("Reopened", "new"),
        _status("Escalated", "new"),
        _status("In Progress", "indeterminate"),
        _status("Done", "done"),
    ]
    proposal = propose_status_map(workflow)
    assert proposal.get("backlog") != "Reopened", (
        "'open' matched inside 'Reopened' — new work would be filed as reopened"
    )
    assert "backlog" not in proposal, (
        f"an ambiguous To Do category should stay unmapped rather than guess: {proposal}"
    )
    assert proposal["active"] == "In Progress", "the unambiguous mappings must still work"


# --- the v2 config must actually persist -------------------------------------


def test_the_v2_fields_survive_a_save_and_reload():
    """SILENT DATA LOSS, caught by the operator asking to see his mapping again.

    ``projects``, ``project_status_maps`` and ``confirmed_projects`` were added to the
    dataclass but wired into NEITHER the serializer NOR the loader NOR the config
    applier. So the projects box never saved, and confirming a project updated memory
    while the UI reported success — "Confirmed IS" was true about RAM and false about
    disk, and the confirmation vanished on the next restart.

    Adding a field to a config model is four changes, not one: model, serializer,
    loader, applier. This asserts the whole round trip rather than any one of them.
    """
    from swarm.config.loader import _parse_jira_section
    from swarm.config.models import HiveConfig
    from swarm.config.serialization import _serialize_jira_optional

    cfg = HiveConfig(session_name="t")
    cfg.jira = JiraConfig(
        enabled=True,
        projects=["WWD", "IS"],
        project_status_maps={"IS": {"done": "Done", "backlog": "ToDo"}},
        confirmed_projects=["IS"],
    )

    out: dict = {}
    _serialize_jira_optional(cfg, out)
    reloaded = _parse_jira_section(out["jira"])

    assert reloaded.projects == ["WWD", "IS"], "the configured projects did not survive"
    assert reloaded.confirmed_projects == ["IS"], (
        "the confirmation did not survive a restart — the sweep would refuse to "
        "converge a project the operator had already approved"
    )
    assert reloaded.project_status_maps["IS"]["done"] == "Done", "the mapping was lost"
    assert reloaded.is_confirmed("IS") is True


def test_the_config_applier_accepts_the_v2_fields():
    """The UI's save path. Without this the projects box round-tripped back to its old
    value and the operator watched their input revert with no error."""
    from swarm.server.config_appliers.jira import _apply_jira_v2_fields

    cfg = JiraConfig(enabled=True)
    consumed: list[str] = []
    _apply_jira_v2_fields(
        cfg,
        {
            "projects": ["WWD", " IS ", ""],
            "confirmed_projects": ["IS"],
            "project_status_maps": {"IS": {"done": "Resolved"}},
        },
        consumed,
    )
    assert cfg.projects == ["WWD", "IS"], "blank/whitespace entries were not cleaned"
    assert cfg.confirmed_projects == ["IS"]
    assert cfg.project_status_maps["IS"]["done"] == "Resolved"
    assert set(consumed) >= {"projects", "confirmed_projects", "project_status_maps"}, (
        f"unconsumed keys are reported as unknown config: {consumed}"
    )


def test_the_new_keys_are_registered_as_known():
    """Otherwise every load logs 'unrecognized key ... (typo?)' for keys the system
    itself writes — a warning that trains the operator to ignore warnings."""
    from swarm.config._known_keys import _KNOWN_JIRA_KEYS

    for key in ("projects", "issue_types", "project_status_maps", "confirmed_projects"):
        assert key in _KNOWN_JIRA_KEYS, f"{key} would be warned about on every load"


# --- dead settings are GONE, not merely disabled -----------------------------


@pytest.mark.parametrize("field", ["import_filter", "import_label", "lookback_days"])
def test_the_dead_settings_are_removed_from_every_layer(field: str):
    """OPERATOR-REPORTED: "removing from the UI (and the backend) what no longer
    applies".

    These three stopped doing anything when imports became assignee-routed:
    import_filter and import_label no longer route, and lookback_days was read by no
    query at all — it was plumbed through loader, applier and known-keys purely to
    reach a field nothing consulted. Disabling them in the UI was not enough: a
    setting the operator can still see reads as configuration even when it is
    decoration, and the backend kept carrying three fields that had to be serialized,
    loaded and validated forever.
    """
    from pathlib import Path as _P

    assert not hasattr(JiraConfig(), field), f"JiraConfig still carries {field}"
    for path in (
        "src/swarm/config/serialization.py",
        "src/swarm/config/loader.py",
        "src/swarm/server/config_appliers/jira.py",
    ):
        src = _P(path).read_text()
        code = "\n".join(ln for ln in src.split("\n") if not ln.strip().startswith("#"))
        assert f'"{field}"' not in code, f"{path} still handles {field}"


@pytest.mark.parametrize("field", ["import_filter", "import_label", "lookback_days"])
def test_removed_settings_are_reported_as_stale_not_as_typos(field: str):
    """An existing config still has these keys. Dropping them from the known set
    without listing them as REMOVED would warn "unrecognized key (typo?)" — telling
    the operator they mistyped something they never touched."""
    from swarm.config._known_keys import _STALE_JIRA_KEYS

    assert field in _STALE_JIRA_KEYS, (
        f"{field} would be reported as a typo rather than as a removed setting"
    )


def test_the_config_page_no_longer_offers_the_dead_settings():
    from pathlib import Path as _P

    page = _P("src/swarm/web/templates/config.html").read_text()
    for field in ("cfg-jira-import_filter", "cfg-jira-import_label", "cfg-jira-lookback_days"):
        assert field not in page, f"{field} is still on the config page"
    # The raw JSON textarea is gone too: hand-editing it is how a map targeting "Done"
    # ended up refused by every ticket in a project with no Done transition.
    assert "cfg-jira-status_map" not in page, "the raw status_map textarea is still present"


def test_saved_mappings_are_rendered_from_config_not_from_a_discover_click():
    """The operator's actual request: seeing what is mapped, after the fact."""
    from pathlib import Path as _P

    page = _P("src/swarm/web/templates/config.html").read_text()
    # Window bounded to the setup BLOCK, not to the panel's own div id: the {% set %}
    # that reads stored config sits just above the div, so anchoring on the id alone
    # excluded the very line under test — the same shape as the scan windows that were
    # mis-sized in both directions earlier in this work.
    block = page[page.index('id="jira-setup-block"') : page.index("Step 3: cadence")]
    assert "project_status_maps" in block, (
        "the saved-mappings panel does not read stored config, so it can only show "
        "what the last Discover click returned"
    )
    assert "confirmed_projects" in block, "confirmation state is not shown"
    assert "Re-discover" in page, "a saved project cannot be re-read without retyping its key"
