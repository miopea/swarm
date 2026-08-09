"""A linked task keeps receiving Jira comments after it is imported.

THE GAP. ``import_issues`` dedupes on ``jira_key`` and SKIPS tasks that already exist,
so comments and attachments were mirrored exactly once — at creation — and never again.
On a service desk the comment thread IS the requirement: a stakeholder writes "actually
the customer needs X" and the worker never saw it, because nothing looked again.

WHY THE OBVIOUS FIX IS DESTRUCTIVE. ``refresh_task`` already existed for the manual
button, and it re-derives the description from the Jira body and REPLACES
``task.attachments``. Putting THAT on a timer would silently delete, every five minutes,
everything a worker wrote into the description and every attachment Swarm added itself
— the #1289 truncation, automated and repeating.

So the scheduled path is additive by construction: it rebuilds only the region below the
``--- Jira sync ---`` marker and MERGES attachments. It can add; it has no path by which
it can remove. Most of this file exists to hold that line.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.config.models import JiraConfig
from swarm.db.core import SwarmDB
from swarm.db.task_store import SqliteTaskStore
from swarm.integrations.jira import JiraSyncService
from swarm.tasks.board import TaskBoard
from swarm.tasks.task import SwarmTask


@pytest.fixture
def board(tmp_path: Path) -> TaskBoard:
    return TaskBoard(store=SqliteTaskStore(SwarmDB(tmp_path / "swarm.db")))


def _svc(tmp_path: Path) -> JiraSyncService:
    mgr = MagicMock()
    mgr.is_connected.return_value = True
    mgr.api_base_url = "https://api.atlassian.com/ex/jira/test"
    svc = JiraSyncService(
        JiraConfig(enabled=True, projects=["WWD"]),
        token_manager=mgr,
        uploads_dir=tmp_path / "uploads",
    )
    assert svc.enabled, "positive control: a disabled service makes every test vacuous"
    return svc


def _issue(comments: list[str], attachments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "fields": {
            "description": "THE JIRA BODY",
            "comment": {
                "comments": [
                    {
                        "author": {"displayName": "Larissa"},
                        "created": "2026-08-09T10:00:00.000+0000",
                        "body": c,
                    }
                    for c in comments
                ]
            },
            "attachment": attachments or [],
        }
    }


def _task(board: TaskBoard, description: str) -> SwarmTask:
    t = board.add(SwarmTask(title="t", description=description))
    board.set_jira_key(t.id, "WWD-1")
    return board.get(t.id)


# --- the thing that was missing ------------------------------------------------


@pytest.mark.asyncio
async def test_a_new_comment_reaches_the_task(tmp_path: Path, board: TaskBoard):
    svc = _svc(tmp_path)
    svc.client.get_issue = AsyncMock(return_value=_issue(["actually the customer needs X"]))
    task = _task(board, "THE JIRA BODY")

    latest = await svc.refresh_synced_content(task)

    assert "actually the customer needs X" in task.description
    assert "Larissa" in latest, f"the caller cannot say WHAT changed: {latest!r}"


@pytest.mark.asyncio
async def test_an_unchanged_ticket_reports_no_change(tmp_path: Path, board: TaskBoard):
    """Every sync cycle calls this for every open linked task. Reporting a change each
    time would message the worker on a five-minute timer, which trains them to ignore
    it — the same noise failure as the export reconciler retrying 11 tickets."""
    svc = _svc(tmp_path)
    svc.client.get_issue = AsyncMock(return_value=_issue(["one comment"]))
    task = _task(board, "THE JIRA BODY")

    assert await svc.refresh_synced_content(task) != ""
    assert await svc.refresh_synced_content(task) == "", "an unchanged ticket reported a change"


# --- it must never remove anything --------------------------------------------


@pytest.mark.asyncio
async def test_worker_authored_description_text_survives(tmp_path: Path, board: TaskBoard):
    """THE DATA-LOSS CASE, and the reason this is not just refresh_task on a timer.

    A worker appends findings to the description. The scheduled refresh must not touch
    a byte of them — #1289 lost 2,200 characters of verified findings to exactly this
    shape, once, by hand. On a timer it would happen every five minutes.
    """
    svc = _svc(tmp_path)
    svc.client.get_issue = AsyncMock(return_value=_issue(["a comment"]))
    findings = "THE JIRA BODY\n\nAC-1 evidence: measured 3,819 chars; root cause is the latch."
    task = _task(board, findings)

    await svc.refresh_synced_content(task)

    assert "AC-1 evidence: measured 3,819 chars; root cause is the latch." in task.description, (
        "the scheduled refresh destroyed worker-authored findings"
    )


@pytest.mark.asyncio
async def test_swarm_side_attachments_survive(tmp_path: Path, board: TaskBoard):
    """refresh_task REPLACES task.attachments. A debugging screenshot a worker captured
    is not in Jira, so replacing the list deletes it."""
    svc = _svc(tmp_path)
    svc.client.get_issue = AsyncMock(return_value=_issue(["c"]))
    task = _task(board, "THE JIRA BODY")
    task.attachments = ["/home/me/screenshot-of-the-bug.png"]

    await svc.refresh_synced_content(task)

    assert "/home/me/screenshot-of-the-bug.png" in task.attachments, (
        "a Swarm-side attachment was deleted by the refresh"
    )


@pytest.mark.asyncio
async def test_repeated_refreshes_do_not_duplicate_the_synced_block(
    tmp_path: Path, board: TaskBoard
):
    """The tail is rebuilt, not appended, so a description cannot grow without bound."""
    svc = _svc(tmp_path)
    svc.client.get_issue = AsyncMock(return_value=_issue(["only comment"]))
    task = _task(board, "THE JIRA BODY")

    for _ in range(3):
        await svc.refresh_synced_content(task)

    assert task.description.count("only comment") == 1, f"duplicated: {task.description!r}"


@pytest.mark.asyncio
async def test_a_failed_fetch_changes_nothing(tmp_path: Path, board: TaskBoard):
    """A refresh that cannot read is a no-op, never a truncation. Rebuilding from an
    empty payload would wipe the synced block on every transient error."""
    svc = _svc(tmp_path)
    svc.client.get_issue = AsyncMock(return_value=_issue(["important context"]))
    task = _task(board, "THE JIRA BODY")
    await svc.refresh_synced_content(task)
    before = task.description

    svc.client.get_issue = AsyncMock(side_effect=RuntimeError("500"))
    assert await svc.refresh_synced_content(task) == ""
    assert task.description == before, "a failed fetch rewrote the description"


# --- the scheduled pass: scope, signal, wiring --------------------------------


def _service(board: TaskBoard, jira: Any, store: Any = None):
    from swarm.server.jira_service import JiraService

    svc = JiraService.__new__(JiraService)
    svc._task_board = board
    svc._get_jira = lambda: jira
    svc._drone_log = MagicMock()
    svc._broadcast_ws = lambda _p: None
    svc._track_task = lambda _t: None
    svc._message_store = store
    return svc


def _jira_reporting(latest: str) -> Any:
    jira = MagicMock()
    jira.enabled = True
    # The sweep BATCHES: one fetch_synced_fields for the whole set, then a per-task
    # apply. Before batching it issued one API call per task — ~123 per cycle on a
    # 55-ticket board (#1350).
    jira.fetch_synced_fields = AsyncMock(side_effect=lambda keys: {k: {} for k in keys})

    async def _apply(task: Any, prefetched: Any = None) -> str:
        # MUTATES, like the real thing. The sweep now persists on an actual description
        # change rather than on the return value, so a mock that reports news without
        # changing anything no longer resembles the code under test.
        if latest:
            task.description = f"{task.description}\n\n--- Jira sync ---\nComments:\n{latest}"
        return latest

    jira.refresh_synced_content = AsyncMock(side_effect=_apply)
    return jira


def _linked(board: TaskBoard, key: str, worker: str = "api") -> SwarmTask:
    t = board.add(SwarmTask(title=f"t {key}", description="body"))
    board.set_jira_key(t.id, key)
    board.assign(t.id, worker)
    return board.get(t.id)


@pytest.mark.asyncio
async def test_the_assigned_worker_is_told_what_changed(board: TaskBoard):
    """Mirroring a comment into a description nobody re-reads is half an answer. The
    worker gets a MESSAGE (inbox, not a PTY interrupt, so it does not cut across
    whatever they are mid-way through saying)."""
    _linked(board, "WWD-1", worker="api")
    store = MagicMock()
    svc = _service(board, _jira_reporting("Larissa: actually the customer needs X"), store)

    assert await svc.refresh_linked_tasks() == 1

    store.send.assert_called_once()
    kwargs = store.send.call_args.kwargs
    assert kwargs["recipient"] == "api"
    assert "actually the customer needs X" in kwargs["content"]
    assert "WWD-1" in kwargs["content"]


@pytest.mark.asyncio
async def test_no_message_when_nothing_changed(board: TaskBoard):
    _linked(board, "WWD-2")
    store = MagicMock()
    svc = _service(board, _jira_reporting(""), store)

    assert await svc.refresh_linked_tasks() == 0
    store.send.assert_not_called()


@pytest.mark.asyncio
async def test_finished_and_disowned_tasks_are_not_refreshed(board: TaskBoard):
    """A closed task cannot act on new information, and a released one is somebody
    else's problem — refreshing either spends API calls to notify nobody."""
    done = _linked(board, "WWD-3")
    board.complete(done.id, "shipped")
    released = _linked(board, "WWD-4")
    board.release(released.id)
    board.update(released.id, tags=["hold"])
    open_one = _linked(board, "WWD-5")

    jira = _jira_reporting("")
    await _service(board, jira).refresh_linked_tasks()

    seen = {c.args[0].jira_key for c in jira.refresh_synced_content.call_args_list}
    assert seen == {"WWD-5"}, f"refreshed tasks it should have skipped: {seen}"
    assert open_one.jira_key == "WWD-5"


@pytest.mark.asyncio
async def test_one_failure_does_not_stop_the_rest(board: TaskBoard):
    """A single unreadable ticket must not silence every other task's comments."""
    _linked(board, "WWD-6", worker="api")
    _linked(board, "WWD-7", worker="web")
    jira = MagicMock()
    jira.enabled = True
    jira.fetch_synced_fields = AsyncMock(side_effect=lambda keys: {k: {} for k in keys})

    async def _apply(task: Any, prefetched: Any = None) -> str:
        if task.jira_key == "WWD-6":
            raise RuntimeError("boom")
        task.description = f"{task.description}\n\n--- Jira sync ---\nComments:\nBob: hi"
        return "Bob: hi"

    jira.refresh_synced_content = AsyncMock(side_effect=_apply)

    assert await _service(board, jira, MagicMock()).refresh_linked_tasks() == 1


@pytest.mark.asyncio
async def test_a_missing_message_store_still_refreshes(board: TaskBoard):
    """The notification is best-effort; losing it must not lose the mirrored comment."""
    _linked(board, "WWD-8")
    svc = _service(board, _jira_reporting("Bob: something"), store=None)
    assert await svc.refresh_linked_tasks() == 1


def test_the_refresh_is_wired_into_the_sync_loop():
    """The wiring, not the function — the shape that has fooled six controls in this
    work. Every other check here calls refresh_linked_tasks() directly."""
    src = Path("src/swarm/server/jira_service.py").read_text()
    loop = src[src.index("async def sync_loop") :]
    loop = loop[: loop.index("except asyncio.CancelledError")]
    assert "refresh_linked_tasks()" in loop, (
        "linked tasks are never refreshed on a schedule, so a comment added after "
        "import still never reaches the worker"
    )


# --- the send() signature, which had a silently-broken caller in production ---


def test_message_store_send_takes_keywords_not_a_Message():
    """FOUND 2026-08-09 by copying the existing pattern and watching it do nothing.

    ``MessageStore.send`` takes ``(sender, recipient, msg_type, content)``. The daemon's
    queen-drift notification passed a ``Message`` OBJECT as the first positional with the
    other required arguments missing, so every call raised TypeError straight into a
    surrounding ``except Exception: _log.debug(...)`` — invisible at the operator's
    default WARNING level. The Queen had never once received that notification.

    Pinned by signature rather than by call site so the next caller cannot repeat it.
    """
    import inspect

    from swarm.messages.store import Message, MessageStore

    params = list(inspect.signature(MessageStore.send).parameters)
    assert params[1:] == ["sender", "recipient", "msg_type", "content"], (
        f"send()'s signature changed; every caller needs revisiting: {params}"
    )
    # Message is NOT constructible the way those callers tried — it needs id and
    # created_at, which is exactly why the TypeError was raised.
    with pytest.raises(TypeError):
        Message(sender="a", recipient="b", msg_type="finding", content="c")


def test_no_caller_passes_a_Message_object_to_send():
    """The sweep. One call site was wrong for months; a signature test alone would not
    have caught it, because the caller type-checks fine at import."""
    import re

    offenders = []
    for path in Path("src/swarm").rglob("*.py"):
        src = path.read_text()
        for m in re.finditer(r"\.send\(\s*\n?\s*Message\(", src):
            offenders.append(f"{path}:{src[: m.start()].count(chr(10)) + 1}")
    assert not offenders, (
        f"these pass a Message object to send(), which raises TypeError into whatever "
        f"except block surrounds it: {offenders}"
    )


# --- the sequence that actually happens, which the fixture above did not ------


@pytest.mark.asyncio
async def test_findings_appended_AFTER_a_sync_still_survive(tmp_path: Path, board: TaskBoard):
    """OBSERVED ON REAL DATA 2026-08-09, and it destroyed the findings.

    `test_worker_authored_description_text_survives` passes and proved nothing about
    this case: its fixture had NO existing sync tail, so the appended text landed above
    the marker. The real sequence is import -> sync -> worker appends -> sync again, and
    by the second sync the description already ENDS with the generated block. Appending
    to the end therefore put the findings BELOW the marker, and the next refresh
    stripped and rebuilt the tail, taking them with it.

    Two features each correct alone: #1289 added append_description precisely so adding
    to a description cannot lose it, and the sync owns everything after the marker.
    Together they deleted exactly what append_description exists to protect.
    """
    from swarm.mcp.handlers._edit import _resolve_description

    svc = _svc(tmp_path)
    svc.client.get_issue = AsyncMock(return_value=_issue(["first comment"]))
    task = _task(board, "THE JIRA BODY")

    # 1. first sync — the description now ends with the generated block
    await svc.refresh_synced_content(task)
    assert "--- Jira sync ---" in task.description

    # 2. the worker appends findings, exactly as swarm_edit_task does
    new_desc, refusal = _resolve_description(task, None, "WORKER FINDINGS: the latch is the cause")
    assert refusal is None
    task.description = new_desc

    # 3. a new comment arrives, so the tail is rebuilt
    svc.client.get_issue = AsyncMock(return_value=_issue(["first comment", "second comment"]))
    await svc.refresh_synced_content(task)

    assert "WORKER FINDINGS: the latch is the cause" in task.description, (
        "the refresh destroyed findings a worker appended after the first sync — the "
        "exact data loss append_description exists to prevent"
    )
    assert "second comment" in task.description, "the new comment did not arrive"


def test_appending_puts_text_above_the_sync_marker(board: TaskBoard):
    """The mechanism, stated directly: anything added must land in the user-authored
    region, because the sync regenerates everything after the marker."""
    from swarm.mcp.handlers._edit import _resolve_description
    from swarm.tasks.task import JIRA_SYNC_MARKER

    task = _task(board, f"BODY{JIRA_SYNC_MARKER}Comments:\n[x] Someone:\nhello")
    new_desc, refusal = _resolve_description(task, None, "MY FINDINGS")

    assert refusal is None
    base, _, tail = new_desc.partition(JIRA_SYNC_MARKER)
    assert "MY FINDINGS" in base, "the addition landed below the marker; the next sync eats it"
    assert "MY FINDINGS" not in tail
    assert "hello" in tail, "the generated tail was not preserved"


def test_appending_to_a_task_with_no_marker_is_unchanged(board: TaskBoard):
    """The common case must not regress: an unlinked task has no marker at all."""
    from swarm.mcp.handlers._edit import _resolve_description

    task = _task(board, "PLAIN BODY")
    new_desc, refusal = _resolve_description(task, None, "MORE")
    assert refusal is None
    assert new_desc == "PLAIN BODY\n\nMORE"


# --- Swarm must not report its own comments as news ---------------------------


@pytest.mark.asyncio
async def test_swarms_own_blocker_note_does_not_notify(tmp_path: Path, board: TaskBoard):
    """OBSERVED LIVE 2026-08-09, in my own inbox:

        [finding] from jira: WWD-6719 (your task #1347) has new activity — Latest
        comment: [swarm:blocker:1347] Swarm is BLOCKED on this: ...

    Posting a blocker note made the comment sync see new activity and message the worker
    about a comment SWARM HAD JUST WRITTEN — twice, once for the block and once for the
    clear. An echo like that trains workers to ignore the notification, which is exactly
    when a real stakeholder comment gets missed.
    """
    svc = _svc(tmp_path)
    svc.client.get_issue = AsyncMock(
        return_value=_issue(["[swarm:blocker:1347] Swarm is BLOCKED on this: waiting"])
    )
    task = _task(board, "THE JIRA BODY")

    latest = await svc.refresh_synced_content(task)

    assert latest == "", "Swarm notified a worker about its own comment"
    assert "Swarm is BLOCKED" in task.description, (
        "the note should still be MIRRORED — only the notification is suppressed, "
        "because the mirror is what a human reads"
    )


@pytest.mark.asyncio
async def test_a_real_comment_still_notifies(tmp_path: Path, board: TaskBoard):
    """The suppression must not become a blanket mute — a stakeholder changing scope is
    the whole reason this feature exists."""
    svc = _svc(tmp_path)
    svc.client.get_issue = AsyncMock(return_value=_issue(["actually the customer needs X"]))
    assert "customer needs X" in await svc.refresh_synced_content(_task(board, "THE JIRA BODY"))


@pytest.mark.asyncio
async def test_a_human_comment_under_an_older_swarm_note_still_notifies(
    tmp_path: Path, board: TaskBoard
):
    """Only the NEWEST comment decides. A swarm note earlier in the thread must not mute
    everything after it."""
    svc = _svc(tmp_path)
    svc.client.get_issue = AsyncMock(
        return_value=_issue(["[swarm:blocker:9] Swarm is BLOCKED on this: x", "please also do Y"])
    )
    assert "do Y" in await svc.refresh_synced_content(_task(board, "THE JIRA BODY"))


def test_every_comment_swarm_writes_carries_the_marker():
    """The blocker note and the worklog already carried markers; the COMPLETION comment
    did not, so it would echo the same way. Swept rather than pinned per call site."""
    import ast

    from swarm.integrations.jira import _SWARM_COMMENT_PREFIX

    src = Path("src/swarm/integrations/jira.py").read_text()
    writers = ("post_completion_comment", "sync_blocker_note", "log_work")
    for name in writers:
        fn = next(
            n
            for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.AsyncFunctionDef) and n.name == name
        )
        # Case-insensitive, and accepts the CONSTANT as well as the literal: the
        # completion comment appends _SWARM_COMMENT_MARKER rather than the raw string,
        # which the first version of this check missed.
        code = ast.unparse(fn).lower()
        assert _SWARM_COMMENT_PREFIX in code or "marker" in code, (
            f"{name} writes to Jira without marking the text as Swarm's, so the comment "
            f"sync will report it back to a worker as new activity"
        )


# --- persistence is separate from notification --------------------------------


@pytest.mark.asyncio
async def test_a_change_with_nothing_to_notify_is_STILL_persisted(board: TaskBoard):
    """FOUND LIVE 2026-08-09 on WWD-6743: the API served a description containing the
    synced block while the DATABASE had none.

    refresh_synced_content mutates the board's own SwarmTask object, and the service
    persisted only when there was ALSO a notifiable comment. So a reporter/due-date
    update — or a ticket whose only new comment is Swarm's own, which the echo fix now
    suppresses — changed memory and never reached the database. It survived until some
    unrelated board write flushed it, and was lost on restart.

    Persist on CHANGE; notify on NEWS. Two questions, not one.
    """
    task = _linked(board, "WWD-1", worker="api")
    store = MagicMock()
    jira = MagicMock()
    jira.enabled = True
    jira.fetch_synced_fields = AsyncMock(side_effect=lambda keys: {k: {} for k in keys})

    async def _mutate(t: Any, prefetched: Any = None) -> str:
        t.description = "body\n\n--- Jira sync ---\nReported by: Larissa"
        return ""  # nothing worth telling a worker about

    jira.refresh_synced_content = AsyncMock(side_effect=_mutate)

    assert await _service(board, jira, store).refresh_linked_tasks() == 1
    assert "Reported by: Larissa" in board.get(task.id).description, (
        "the synced block never reached the board, so it is lost on restart"
    )
    store.send.assert_not_called(), "it notified about a change with no news"


@pytest.mark.asyncio
async def test_no_change_persists_nothing(board: TaskBoard):
    """The sweep runs every cycle for every open linked task; writing unconditionally
    would churn the board and its broadcasts forever."""
    _linked(board, "WWD-2", worker="api")
    jira = MagicMock()
    jira.enabled = True
    jira.fetch_synced_fields = AsyncMock(side_effect=lambda keys: {k: {} for k in keys})
    jira.refresh_synced_content = AsyncMock(return_value="")

    assert await _service(board, jira, MagicMock()).refresh_linked_tasks() == 0
