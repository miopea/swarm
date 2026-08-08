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


def test_an_unmapped_project_gets_NO_map_rather_than_a_global_one():
    """REVERSED DELIBERATELY 2026.8.8.9 — this test used to assert the opposite.

    It pinned a global fallback justified as upgrade safety: a v1 install has a global
    map and no per-project maps, so refusing to export until every project was
    re-confirmed would break a working integration.

    That reasoning was defeated by the field's own default. ``status_map`` defaulted to
    a FULL hardcoded map, so the fallback never returned empty: every unmapped project
    silently received ``done -> "Done"``, on every install including fresh ones that had
    never configured Jira. "Genuine v1 config" and "nobody ever touched this" were
    indistinguishable, so the compatibility case the fallback existed for could not be
    told apart from the case it broke.

    The old test could not catch that because it CONSTRUCTED the global map it then
    asserted on — it never asked what an unconfigured install would get.
    """
    cfg = JiraConfig(enabled=True, projects=["WWD"])
    assert cfg.status_map_for("WWD") == {}, (
        "an unmapped project still inherits a map it never confirmed"
    )
    assert cfg.status_map_for("ANYTHING") == {}


def test_a_fresh_config_maps_nothing_at_all():
    """The case the old test never asked about, and the reason the fallback was
    invisible for so long."""
    assert JiraConfig().status_map_for("WWD") == {}, (
        "a brand-new install ships a hardcoded transition map for every project"
    )


def test_a_confirmed_project_is_unaffected_by_another_projects_map():
    """Strictness must not become a wall: mapping still has to work."""
    cfg = JiraConfig(
        enabled=True,
        projects=["WWD", "IS"],
        project_status_maps={"WWD": {"done": "Done"}},
    )
    assert cfg.status_map_for("WWD") == {"done": "Done"}
    assert cfg.status_map_for("IS") == {}, "IS inherited WWD's map"


def test_the_returned_map_is_a_copy():
    """A caller mutating the result must not silently rewrite stored config — the
    lookup is on the export path and runs on every transition."""
    cfg = JiraConfig(enabled=True, project_status_maps={"WWD": {"done": "Done"}})
    cfg.status_map_for("WWD")["done"] = "Wrong"
    assert cfg.project_status_maps["WWD"]["done"] == "Done", (
        "the export path can mutate stored configuration"
    )


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


def _config_page() -> str:
    from pathlib import Path as _P

    return _P("src/swarm/web/templates/config.html").read_text()


def _page_code() -> str:
    """The page with whole-line comments blanked.

    Scans in this repo have repeatedly matched the PROSE EXPLAINING A CHANGE rather
    than the change — three times now — and the comments added with this fix name
    both the function and the endpoint under test.
    """
    return "\n".join(
        ln
        for ln in _config_page().split("\n")
        if not ln.strip().startswith(("//", "{#", "*", "<!--"))
    )


def test_the_saved_mappings_panel_has_exactly_one_renderer():
    """OPERATOR-REPORTED 2026-08-08: "after I save discover the map list doesn't update.
    I have to refresh the page."

    The panel was a Jinja loop, so it was built ONCE at page load: confirming a workflow
    saved the config and the operator kept looking at the old table until they
    refreshed. The fix is not to push an update after confirm — that is the patch shape
    that kept failing on the task board — but to make the panel RE-READ the authority,
    so it is right even after an update nobody thought to send.

    Two renderers for one panel is the specific regression: a Jinja version and a JS
    version drift, and the server-rendered one wins at page load.
    """
    setup = _config_page()
    block = setup[setup.index('id="jira-setup-block"') : setup.index("Step 3: cadence")]
    assert "{% for proj" not in block, (
        "the Jinja loop is back alongside the JS renderer; the two will drift and the "
        "server-rendered table wins at page load"
    )
    assert "jira-maps-body" in block, "the panel has no container for the JS renderer"


def test_the_panel_reloads_after_a_confirm_and_on_load():
    code = _page_code()
    confirm = code[code.index("function jiraConfirm(") : code.index("function jiraPlan(")]
    assert "jiraLoadSavedMaps()" in confirm, (
        "confirming does not refresh the saved-mappings table, so the operator still "
        "has to reload the page — the reported bug"
    )
    # Three call sites: confirm, tab-switch, and page load. The load call matters
    # because the Integrations tab can be the one already open, in which case no switch
    # event ever fires and the table would sit on "Loading…" forever.
    assert code.count("jiraLoadSavedMaps()") >= 3, (
        f"expected calls from confirm, tab-switch and load; found "
        f"{code.count('jiraLoadSavedMaps()')}"
    )


def test_the_renderer_re_reads_the_api_rather_than_applying_the_confirm_response():
    """Applying what confirm returned would leave the panel correct only for the updates
    somebody remembered to send. Re-reading recovers from the ones they did not."""
    page = _config_page()
    fn = page[
        page.index("function jiraLoadSavedMaps(") : page.index("function _jiraRenderSavedMaps(")
    ]
    assert "/api/jira/mappings" in fn, "the panel does not re-read the authority"
    assert ".catch(" in fn, (
        "a failed mappings read leaves the table on 'Loading…', which reads as "
        "'nothing configured' — the opposite of 'I could not tell you'"
    )


def test_only_one_switchConfigTab_decorator_exists():
    """#1292's shape: two `var _orig… = switchConfigTab` wrappers, and whichever
    assignment runs last silently discards the other's behaviour."""
    code = _page_code()
    assert code.count("_origSwitchConfigTab = switchConfigTab") == 1, (
        "a second tab-switch decorator was added; one of them will be discarded"
    )


def test_unmapped_states_are_shown_rather_than_omitted():
    """An unmapped Swarm state is not cosmetic: export_status refuses the transition, so
    that state silently never reaches Jira. Omitting it from the row makes 'not mapped'
    indistinguishable from 'not shown'."""
    page = _config_page()
    fn = page[page.index("function _jiraRenderSavedMaps(") :]
    fn = fn[: fn.index("\n    function ")]
    assert "unmapped" in fn, "the renderer never surfaces unmapped states"
    assert "not mapped" in fn, "there is no visible label for an unmapped state"


# --- the mappings endpoint ---------------------------------------------------
#
# The scans above prove the PANEL re-reads an endpoint. They cannot prove the endpoint
# answers correctly, and a panel faithfully rendering wrong data is still wrong.


def _mappings(cfg: JiraConfig) -> list[dict]:
    """Call the handler against a config, without a daemon or a network."""
    import asyncio
    import json as _json
    from types import SimpleNamespace

    from swarm.server.routes.jira import handle_jira_mappings

    class _Req:
        """get_daemon() reads request.app["daemon"] and nothing else."""

        def __init__(self, daemon):
            self.app = {"daemon": daemon}

    daemon = SimpleNamespace(jira=SimpleNamespace(_config=cfg))
    resp = asyncio.run(handle_jira_mappings(_Req(daemon)))
    return _json.loads(resp.body.decode())["rows"]


def test_the_endpoint_reports_a_row_per_configured_project_not_per_mapped_one():
    """The case the operator could not previously see. A project listed in `projects`
    with no map imports issues and silently exports NOTHING — before this it simply did
    not appear in the table, indistinguishable from not being configured."""
    cfg = JiraConfig(
        enabled=True,
        projects=["WWD", "NEW"],
        project_status_maps={"WWD": {"done": "Done"}},
        confirmed_projects=["WWD"],
    )
    rows = {r["project"]: r for r in _mappings(cfg)}
    assert set(rows) == {"WWD", "NEW"}, f"an unmapped configured project is invisible: {rows}"
    assert rows["NEW"]["status_map"] == {}
    assert rows["NEW"]["confirmed"] is False


def test_unmapped_states_are_named_so_the_gap_is_visible():
    """export_status refuses a transition whose target is absent, so an unmapped state
    never reaches Jira. Naming them is the difference between a visible gap and silence."""
    cfg = JiraConfig(
        enabled=True,
        projects=["IS"],
        project_status_maps={"IS": {"done": "Resolved", "active": "In Progress"}},
    )
    row = _mappings(cfg)[0]
    assert "blocked" in row["unmapped"], f"the unmapped states are not reported: {row}"
    assert "done" not in row["unmapped"], "a mapped state was reported as unmapped"


def test_a_map_kept_for_a_project_no_longer_in_scope_is_still_shown():
    """The map is still stored and applies again the moment the key is re-added, so
    hiding it is a lie of omission."""
    cfg = JiraConfig(enabled=True, projects=["WWD"], project_status_maps={"OLD": {"done": "Done"}})
    rows = {r["project"]: r for r in _mappings(cfg)}
    assert "OLD" in rows, "a stored map vanished from the view because its key left scope"
    assert rows["OLD"]["in_scope"] is False
    assert rows["WWD"]["in_scope"] is True


def test_the_legacy_single_project_field_still_produces_a_row():
    """A v1 install has `project`, not `projects`; it must not read as unconfigured."""
    cfg = JiraConfig(enabled=True, project="WWD")
    rows = [r["project"] for r in _mappings(cfg)]
    assert rows == ["WWD"], f"a legacy install shows no projects at all: {rows}"


# --- an unmapped project refuses, and SAYS SO ---------------------------------


def _svc(cfg: JiraConfig):
    """A service that is genuinely ENABLED, with the Jira HTTP calls stubbed.

    The token manager is not optional scaffolding. `export_status` returns early when
    `enabled` is False, and `enabled` requires a connected token manager — so a service
    built without one refuses everything for a reason that has nothing to do with the
    status map. The first version of these tests omitted it, and the unmapped-project
    test passed while proving only that a disconnected service does nothing.

    The stubs are on the HTTP client, NOT on the mapping lookup under test.
    """
    from unittest.mock import AsyncMock, MagicMock

    from swarm.integrations.jira import JiraSyncService

    mgr = MagicMock()
    mgr.is_connected.return_value = True
    mgr.api_base_url = "https://api.atlassian.com/ex/jira/test-cloud"
    svc = JiraSyncService(cfg, token_manager=mgr)
    assert svc.enabled, (
        "positive control: the service must be enabled or every test here passes vacuously"
    )
    svc.client.get_transitions = AsyncMock(return_value=[{"id": "31", "name": "Done"}])
    svc.client.transition_issue = AsyncMock(return_value=True)
    return svc


@pytest.mark.asyncio
async def test_an_unmapped_project_is_not_transitioned_at_all():
    """THE HAZARD, and it is worse than the IS refusal that started this.

    The inherited global map named "Done". If the target project's workflow happens to
    HAVE a "Done" transition — most do — the export succeeds and moves someone's ticket
    to a state nobody chose, reporting success. The IS case only looked like the whole
    problem because that workflow had no Done transition to hit.
    """
    from swarm.tasks.task import SwarmTask, TaskStatus

    cfg = JiraConfig(enabled=True, projects=["OTHER"])  # never mapped
    svc = _svc(cfg)
    task = SwarmTask(title="t", jira_key="OTHER-1")

    ok = await svc.export_status(task, TaskStatus.DONE)

    assert ok is False, "an unmapped project was transitioned anyway"
    svc.client.transition_issue.assert_not_called()
    # It must not even ASK, so a misconfiguration costs no API calls either.
    svc.client.get_transitions.assert_not_called()


@pytest.mark.asyncio
async def test_the_refusal_is_logged_at_warning_with_what_to_do(caplog):
    """It was DEBUG. Operators run at default WARNING, so a task moving in Swarm while
    its ticket silently did not move in Jira produced nothing anyone would see."""
    import logging

    from swarm.tasks.task import SwarmTask, TaskStatus

    svc = _svc(JiraConfig(enabled=True, projects=["OTHER"]))
    with caplog.at_level(logging.WARNING):
        await svc.export_status(SwarmTask(title="t", jira_key="OTHER-9"), TaskStatus.DONE)

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "a silent no-op on a state change produced no operator-visible log"
    joined = " ".join(warnings)
    assert "OTHER" in joined and "OTHER-9" in joined, (
        f"the log names neither project nor ticket: {joined}"
    )
    assert "confirm" in joined.lower() or "discover" in joined.lower(), (
        f"the warning does not say how to fix it: {joined}"
    )


@pytest.mark.asyncio
async def test_the_warning_fires_once_per_pair_not_every_transition():
    """A discovered map legitimately omits states it could not justify, and export runs
    on every transition — warning each time would bury the signal, which is the same
    noise problem the export reconciler hit with 11 tickets retried every sync."""
    import logging

    from swarm.tasks.task import SwarmTask, TaskStatus

    svc = _svc(JiraConfig(enabled=True, projects=["OTHER"]))
    seen: list[str] = []

    class _Cap(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.WARNING:
                seen.append(record.getMessage())

    logger = logging.getLogger("swarm.integrations.jira")
    handler = _Cap()
    logger.addHandler(handler)
    try:
        for _ in range(4):
            await svc.export_status(SwarmTask(title="t", jira_key="OTHER-1"), TaskStatus.DONE)
        # A DIFFERENT status is a different gap and must warn on its own.
        await svc.export_status(SwarmTask(title="t", jira_key="OTHER-2"), TaskStatus.ACTIVE)
    finally:
        logger.removeHandler(handler)

    assert len(seen) == 2, (
        f"expected one warning per (project, status) pair, got {len(seen)}: {seen}"
    )


@pytest.mark.asyncio
async def test_a_confirmed_project_still_exports():
    """The gate must be a gate, not a wall."""
    from swarm.tasks.task import SwarmTask, TaskStatus

    cfg = JiraConfig(
        enabled=True,
        projects=["WWD"],
        project_status_maps={"WWD": {"done": "Done"}},
        confirmed_projects=["WWD"],
    )
    svc = _svc(cfg)
    assert await svc.export_status(SwarmTask(title="t", jira_key="WWD-1"), TaskStatus.DONE) is True
    svc.client.transition_issue.assert_called_once_with("WWD-1", "31")


def test_a_project_with_linked_tasks_but_no_config_is_still_listed():
    """MTR-11806: a real task linked to a real ticket in a project that is in neither
    `projects` nor `project_status_maps`. It was invisible on the setup screen while the
    reconciler warned about it every five minutes — the one project the board is already
    entangled with, and the only one the operator could not see."""
    import asyncio
    import json as _json
    from types import SimpleNamespace

    from swarm.server.routes.jira import handle_jira_mappings

    class _Req:
        def __init__(self, daemon):
            self.app = {"daemon": daemon}

    task = SimpleNamespace(jira_key="MTR-11806")
    daemon = SimpleNamespace(
        jira=SimpleNamespace(_config=JiraConfig(enabled=True, projects=["WWD"])),
        task_board=SimpleNamespace(all_tasks=[task]),
    )
    rows = {
        r["project"]: r
        for r in _json.loads(asyncio.run(handle_jira_mappings(_Req(daemon))).body)["rows"]
    }

    assert "MTR" in rows, f"a project with linked tasks is invisible on the setup screen: {rows}"
    assert rows["MTR"]["linked_tasks"] == 1
    assert rows["MTR"]["in_scope"] is False
    assert rows["WWD"]["linked_tasks"] == 0
