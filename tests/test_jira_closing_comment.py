"""The comment Swarm leaves on a ticket when work finishes.

This is the ONLY place Swarm's words reach a customer-visible ticket, and it was the
last open question from the v2 interview: "confirm this format against the team's
documented standard responses."

THERE IS NO SUCH DOCUMENT — I looked. So the house style was MEASURED from the team's
own resolved service-desk tickets instead:

    "The issue has been resolved. Removed Power automate from the pc."
    "The site is now accessible. Mr. Schleifer did a workaround for a problem on
     their end."
    "Hello Mr. Schleifer, ... Thank you, William Erik"

Short, plain, addressed to the reporter, signed off. rcg-architecture's org-preferences
adds: "professional, ministry-oriented". The previous template matched none of that — it
opened with "*Task completed in Swarm.*", named the internal worker ("rcg-networks"),
and appended the entire resolution, which runs to 3,800 characters on real linked tasks.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.config.models import JiraConfig
from swarm.integrations.jira import (
    _MAX_RESOLUTION_CHARS,
    JiraSyncService,
    _reporter_from_description,
    _resolution_summary,
)
from swarm.tasks.task import SwarmTask

_SYNCED = "body\n\n--- Jira sync ---\nReported by: Bradford Schleifer\nDue: 2026-08-20"


def _svc() -> JiraSyncService:
    mgr = MagicMock()
    mgr.is_connected.return_value = True
    mgr.api_base_url = "https://api.atlassian.com/ex/jira/test"
    svc = JiraSyncService(JiraConfig(enabled=True, projects=["IS"]), token_manager=mgr)
    assert svc.enabled, "positive control: a disabled service makes every test vacuous"
    svc.client.add_comment = AsyncMock(return_value=True)
    return svc


def _task(**kw: Any) -> SwarmTask:
    t = SwarmTask(
        title=kw.get("title", "Computer locks after inactivity"),
        description=kw.get("description", _SYNCED),
    )
    t.jira_key = "IS-1"
    t.assigned_worker = kw.get("worker", "rcg-networks")
    t.resolution = kw.get("resolution", "The screen lock timeout was reset to 15 minutes.")
    return t


async def _body(svc: JiraSyncService, task: SwarmTask) -> str:
    await svc.post_completion_comment(task)
    return svc.client.add_comment.await_args.args[1]


@pytest.mark.asyncio
async def test_it_addresses_the_reporter_by_name():
    """House style opens "Hello Mr. Schleifer,". We import the reporter now, so the
    comment can do the same instead of addressing nobody."""
    assert "Hello Bradford Schleifer," in await _body(_svc(), _task())


@pytest.mark.asyncio
async def test_it_leads_with_the_outcome_in_plain_language():
    body = await _body(_svc(), _task())
    assert "This has been resolved: Computer locks after inactivity" in body


@pytest.mark.asyncio
async def test_it_does_NOT_name_the_internal_worker():
    """ "*Worker:* rcg-networks" means nothing to the person who raised the ticket, and
    exposes internal routing on a customer-visible thread."""
    assert "rcg-networks" not in await _body(_svc(), _task())


@pytest.mark.asyncio
async def test_it_does_not_say_Task_completed_in_Swarm():
    """Internal jargon. The reporter cares that their problem is fixed."""
    assert "Task completed in Swarm" not in await _body(_svc(), _task())


@pytest.mark.asyncio
async def test_a_long_resolution_is_NOT_dumped_verbatim():
    """THE SIZE PROBLEM, measured: real linked tasks carry resolutions up to 3,804
    characters. They are written for the NEXT WORKER and become learnings — posting one
    to a service-desk ticket is wrong in kind, not merely in length."""
    long_res = "Fixed the tenant resolver. " + ("implementation detail. " * 400)
    body = await _body(_svc(), _task(resolution=long_res))

    assert len(body) < 1200, f"the comment is {len(body)} chars; it dumps the resolution"
    assert "Fixed the tenant resolver." in body, "the summary sentence was lost"
    assert "…" in body, "truncation is silent; the reader cannot tell there is more"


@pytest.mark.asyncio
async def test_an_unlinked_or_unsynced_task_simply_has_no_greeting():
    """No reporter yet is normal, not an error — the greeting is dropped and the rest
    still reads correctly."""
    body = await _body(_svc(), _task(description="plain body, never synced"))
    assert "Hello" not in body
    assert "This has been resolved:" in body


@pytest.mark.asyncio
async def test_it_is_signed_and_honest_about_being_automated():
    """House style signs off. And the reader should know an agent wrote it — the same
    provenance principle behind the reserved `swarm` label."""
    body = await _body(_svc(), _task())
    assert "Thank you," in body
    assert "automated" in body.lower()


@pytest.mark.asyncio
async def test_it_still_carries_the_swarm_marker():
    """Or the comment sync reports Swarm's own words back to a worker as new activity."""
    from swarm.integrations.jira import _SWARM_COMMENT_PREFIX

    assert _SWARM_COMMENT_PREFIX in await _body(_svc(), _task())


# --- the helpers ---------------------------------------------------------------


def test_the_reporter_is_read_from_the_synced_block():
    assert _reporter_from_description(_SYNCED) == "Bradford Schleifer"
    assert _reporter_from_description("no synced block here") == ""


def test_only_the_first_paragraph_of_a_resolution_is_used():
    """The first paragraph is the summary; everything after it is implementation detail
    for the next worker."""
    res = (
        "VERIFIED ALREADY FIXED — closed on evidence.\n\nRoot cause: a hidden Console lock timeout."
    )
    out = _resolution_summary(res)
    assert out == "VERIFIED ALREADY FIXED — closed on evidence."
    assert "Root cause" not in out


def test_truncation_breaks_on_a_word_boundary():
    out = _resolution_summary("word " * 500)
    assert len(out) <= _MAX_RESOLUTION_CHARS + 1
    assert not out.rstrip("…").endswith("wor"), "cut mid-word"


def test_an_empty_resolution_adds_no_empty_section():
    assert _resolution_summary("") == ""
    assert _resolution_summary("   ") == ""
