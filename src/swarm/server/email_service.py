"""EmailService — handles email processing, attachments, and draft replies."""

from __future__ import annotations

import base64
import hashlib
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from swarm.drones.log import LogCategory, SystemAction
from swarm.logging import get_logger
from swarm.paths import state_dir
from swarm.tasks.task import auto_classify_type, smart_title

if TYPE_CHECKING:
    from collections.abc import Callable

    from swarm.auth.graph import GraphTokenManager
    from swarm.drones.log import DroneLog
    from swarm.queen.queen import Queen

_log = get_logger("server.email")

_FETCH_TIMEOUT = 10  # seconds

# Truncation lengths for log messages and notifications
_DIGEST_LEN = 12  # hex prefix for attachment filenames
_TITLE_LOG_LEN = 50  # task title in log.info messages
_TITLE_PREVIEW_LEN = 60  # task title in notification/error previews
_TITLE_NOTIFY_LEN = 80  # task title in drone log entries
_ERROR_PREVIEW_LEN = 80  # error message preview in notifications
_ERROR_LOG_LEN = 200  # error message in broad-catch logging
_MSG_ID_PREVIEW_LEN = 60  # message ID preview in warning messages


_INLINE_MARKS: dict[str, tuple[str, str]] = {
    "b": ("**", "**"),
    "strong": ("**", "**"),
    "i": ("*", "*"),
    "em": ("*", "*"),
    "code": ("`", "`"),
    "s": ("~~", "~~"),
    "strike": ("~~", "~~"),
    "del": ("~~", "~~"),
}

_BLOCK_TAGS = frozenset(
    {"p", "div", "section", "article", "header", "footer", "main", "blockquote"}
)
# Container tags whose content should be skipped — only tags that emit a
# matching end-tag belong here. ``<meta>`` and ``<link>`` are *void* elements:
# HTMLParser never calls handle_endtag for them, so including them here would
# permanently elevate ``_skip_depth`` and silently drop the entire ``<body>``.
# They have no text content anyway, so omitting them is safe.
_SKIP_TAGS = frozenset({"script", "style", "head", "title"})


class _HtmlToMarkdownParser(HTMLParser):
    """Stream HTML → Markdown. Class-based so we can keep state across the
    feed() calls HTMLParser uses internally for chunked input."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._lines: list[str] = [""]
        self._list_stack: list[dict[str, Any]] = []  # [{"type": "ul"|"ol", "idx": N}]
        self._skip_depth = 0
        self._in_pre = False
        self._link_href: list[str] = []  # stack so nested anchors don't clobber

    # ----- output helpers (mirror the ADF helpers in integrations.jira) -----

    def _cur(self) -> str:
        return self._lines[-1]

    def _set(self, line: str) -> None:
        self._lines[-1] = line

    def _push(self) -> None:
        self._lines.append("")

    def _append(self, text: str) -> None:
        self._set(self._cur() + text)

    def _ensure_blank_before(self) -> None:
        if len(self._lines) == 1 and self._lines[0] == "":
            return
        if self._cur() != "":
            self._push()
        if len(self._lines) >= 2 and self._lines[-2] != "":
            self._push()

    # ----- per-tag start handlers -----

    def _start_heading(self, tag: str, _attrs: dict[str, str]) -> None:
        self._ensure_blank_before()
        self._push()
        self._set("#" * int(tag[1]) + " ")

    def _start_block(self, _tag: str, _attrs: dict[str, str]) -> None:
        self._ensure_blank_before()
        self._push()

    def _start_list(self, tag: str, _attrs: dict[str, str]) -> None:
        self._ensure_blank_before()
        self._push()
        self._list_stack.append({"type": tag, "idx": 0})

    def _start_li(self, _tag: str, _attrs: dict[str, str]) -> None:
        if not self._list_stack:
            self._list_stack.append({"type": "ul", "idx": 0})
        top = self._list_stack[-1]
        indent = "  " * (len(self._list_stack) - 1)
        if top["type"] == "ol":
            top["idx"] += 1
            marker = f"{top['idx']}. "
        else:
            marker = "- "
        if self._cur() != "":
            self._push()
        self._set(indent + marker)

    def _start_pre(self, _tag: str, _attrs: dict[str, str]) -> None:
        self._ensure_blank_before()
        self._push()
        self._set("```")
        self._push()
        self._in_pre = True

    def _start_a(self, _tag: str, attrs: dict[str, str]) -> None:
        self._link_href.append(attrs.get("href", ""))
        self._append("[")

    def _start_img(self, _tag: str, attrs: dict[str, str]) -> None:
        src = attrs.get("src", "")
        alt = (attrs.get("alt", "") or "image").replace("[", "").replace("]", "")
        if src:
            self._append(f"![{alt}]({src})")

    def _start_inline_mark(self, tag: str, _attrs: dict[str, str]) -> None:
        self._append(_INLINE_MARKS[tag][0])

    def _start_br(self, _tag: str, _attrs: dict[str, str]) -> None:
        self._push()

    def _start_hr(self, _tag: str, _attrs: dict[str, str]) -> None:
        self._ensure_blank_before()
        self._push()
        self._set("---")
        self._push()

    # ----- HTMLParser callbacks -----

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        attrs_dict = {k: (v or "") for k, v in attrs}
        handler = self._START_DISPATCH.get(tag)
        if handler is not None:
            handler(self, tag, attrs_dict)
            return
        if tag in _INLINE_MARKS:
            self._start_inline_mark(tag, attrs_dict)
            return
        if tag in _BLOCK_TAGS:
            self._start_block(tag, attrs_dict)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        handler = self._END_DISPATCH.get(tag)
        if handler is not None:
            handler(self, tag)
            return
        if tag in _INLINE_MARKS:
            self._end_inline_mark(tag)
            return
        if tag in _BLOCK_TAGS:
            self._end_block(tag)

    # ----- per-tag end handlers -----

    def _end_heading(self, _tag: str) -> None:
        self._push()

    def _end_block(self, _tag: str) -> None:
        if self._cur() != "":
            self._push()

    def _end_list(self, _tag: str) -> None:
        if self._list_stack:
            self._list_stack.pop()
        if not self._list_stack:
            self._push()

    def _end_li(self, _tag: str) -> None:
        if self._cur() != "":
            self._push()

    def _end_pre(self, _tag: str) -> None:
        if self._cur() != "":
            self._push()
        self._set("```")
        self._push()
        self._in_pre = False

    def _end_a(self, _tag: str) -> None:
        href = self._link_href.pop() if self._link_href else ""
        if href:
            self._append(f"]({href})")
            return
        # No href — drop the leading '[' we emitted, keep the inner text.
        cur = self._cur()
        idx = cur.rfind("[")
        if idx != -1:
            self._set(cur[:idx] + cur[idx + 1 :])

    def _end_inline_mark(self, tag: str) -> None:
        self._append(_INLINE_MARKS[tag][1])

    _START_DISPATCH: ClassVar[dict[str, Any]] = {
        "br": _start_br,
        "hr": _start_hr,
        "h1": _start_heading,
        "h2": _start_heading,
        "h3": _start_heading,
        "h4": _start_heading,
        "h5": _start_heading,
        "h6": _start_heading,
        "ul": _start_list,
        "ol": _start_list,
        "li": _start_li,
        "pre": _start_pre,
        "a": _start_a,
        "img": _start_img,
    }

    _END_DISPATCH: ClassVar[dict[str, Any]] = {
        "h1": _end_heading,
        "h2": _end_heading,
        "h3": _end_heading,
        "h4": _end_heading,
        "h5": _end_heading,
        "h6": _end_heading,
        "ul": _end_list,
        "ol": _end_list,
        "li": _end_li,
        "pre": _end_pre,
        "a": _end_a,
    }

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if not data:
            return
        if self._in_pre:
            # Preserve exact whitespace inside <pre> blocks.
            for i, line in enumerate(data.split("\n")):
                if i:
                    self._push()
                self._append(line)
            return
        # Collapse internal whitespace in flow text.
        text = re.sub(r"[\t\n\r ]+", " ", data)
        if text == " " and self._cur() == "":
            return
        self._append(text)

    # ----- finalize -----

    def result(self) -> str:
        text = "\n".join(self._lines)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r" {2,}", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def _html_to_text(html_str: str) -> str:
    """Convert an HTML email body to Markdown.

    Walks the parse tree with stdlib :class:`html.parser.HTMLParser` and
    emits Markdown — paragraphs, headings (with their level preserved),
    bullet/ordered lists with nesting, blockquotes, fenced code blocks,
    horizontal rules — plus inline marks (`**bold**`, `*italic*`,
    `` `code` ``, `[text](url)`). Mirrors the ADF→markdown pass on the
    Jira side so emails and Jira issues land in tasks with the same
    formatting fidelity.
    """
    parser = _HtmlToMarkdownParser()
    parser.feed(html_str or "")
    parser.close()
    return parser.result()


# Outlook (Office 365) defaults to Aptos as the body font; older mail
# clients fall back to Calibri then the generic sans-serif. Reply bodies
# are wrapped in this style so the inserted comment matches the user's
# normal Outlook composition rather than rendering in the recipient's
# default proportional font (often Times New Roman in older clients).
_REPLY_FONT_STYLE = "font-family: Aptos, Calibri, 'Segoe UI', sans-serif; font-size: 12pt;"


def _format_reply_html(plain_text: str) -> str:
    """Wrap the Queen's plain-text reply in HTML styled Aptos 12pt.

    Microsoft Graph's ``createReplyAll`` ``comment`` field accepts HTML
    when the original message body is HTML (Outlook's default). The
    wrapper uses an inline style — Outlook's Word-based renderer
    ignores ``<style>`` blocks but honours ``style=""`` attributes on
    block elements. ``<br>`` preserves the multi-paragraph shape of
    the Queen's draft.

    Empty input returns an empty string so the failure-path callers
    that already pass through unwrapped text don't get a stray empty
    ``<div>`` in the draft.
    """
    import html as _html_mod

    if not plain_text or not plain_text.strip():
        return ""
    escaped = _html_mod.escape(plain_text.strip())
    body = escaped.replace("\n", "<br>")
    return f'<div style="{_REPLY_FONT_STYLE}">{body}</div>'


class EmailService:
    """Handles email-related operations: attachments, processing, and draft replies."""

    def __init__(
        self,
        drone_log: DroneLog,
        queen: Queen,
        graph_mgr: GraphTokenManager | None,
        broadcast_ws: Callable[[dict[str, Any]], None],
        uploads_dir: Path | None = None,
    ) -> None:
        self._drone_log = drone_log
        self._queen = queen
        self._graph_mgr = graph_mgr
        self._broadcast_ws = broadcast_ws
        self._uploads_dir = (uploads_dir or (state_dir() / "uploads")).resolve()

    _SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9._-]")

    def save_attachment(self, filename: str, data: bytes) -> str:
        """Save an uploaded file to uploads dir and return the absolute path."""
        self._uploads_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(data).hexdigest()[:_DIGEST_LEN]
        # Strip directory components, then restrict to safe characters
        base = Path(filename).name
        safe_name = self._SAFE_FILENAME_RE.sub("_", base).strip("_") or "attachment"
        dest = (self._uploads_dir / f"{digest}_{safe_name}").resolve()
        if not dest.is_relative_to(self._uploads_dir):
            raise ValueError(f"Upload path escapes uploads directory: {dest}")
        dest.write_bytes(data)
        return str(dest)

    async def fetch_and_save_image(self, url: str) -> str:
        """Fetch an image from a URL and save as attachment. Returns saved path."""
        import aiohttp as _aiohttp

        from swarm.server.daemon import SwarmOperationError

        if url.startswith("blob:"):
            raise ValueError("blob: URLs cannot be fetched")
        if not url.startswith(("http://", "https://", "file:///")):
            raise ValueError("unsupported URL scheme")

        if url.startswith("file:///"):
            from urllib.parse import unquote as _unquote

            local_path = _unquote(url[8:])  # strip file:///
            # Convert Windows path to WSL if needed
            if len(local_path) > 1 and local_path[1] == ":":
                drive = local_path[0].lower()
                local_path = f"/mnt/{drive}{local_path[2:].replace(chr(92), '/')}"
            fp = Path(local_path).resolve()
            # Restrict file:// reads to the uploads directory (prevent path traversal)
            uploads_resolved = self._uploads_dir.resolve()
            if not fp.is_relative_to(uploads_resolved):
                raise ValueError(f"file:/// access denied outside uploads dir: {fp}")
            if not fp.exists():
                raise FileNotFoundError(f"file not found: {local_path}")
            img_data = fp.read_bytes()
            filename = fp.name
        else:
            async with _aiohttp.ClientSession() as sess:
                timeout = _aiohttp.ClientTimeout(total=_FETCH_TIMEOUT)
                async with sess.get(url, timeout=timeout) as resp:
                    if resp.status != 200:
                        raise SwarmOperationError(f"HTTP {resp.status}")
                    img_data = await resp.read()
                    filename = url.split("/")[-1].split("?")[0] or "image.png"

        if not img_data:
            raise SwarmOperationError("empty response from URL")

        return self.save_attachment(filename, img_data)

    async def process_email_data(
        self,
        subject: str,
        body_content: str,
        body_type: str,
        attachment_dicts: list[dict[str, Any]],
        effective_id: str,
        *,
        graph_token: str = "",
    ) -> dict[str, Any]:
        """Process fetched email data into task fields.

        Converts HTML to text, saves attachments, generates title, classifies type.
        Returns a dict ready for the frontend task-creation form.
        """
        if body_type.lower() == "html":
            body_text = _html_to_text(body_content)
        else:
            body_text = body_content.strip()

        # Save every attachment to disk first. Track inline attachments
        # (Outlook embeds images via cid: refs in the HTML body, then ships
        # the bytes in the attachment list with isInline=true + contentId).
        # The cid:<id> markers in body_text get rewritten to /uploads/<file>
        # so the rendered preview shows them inline instead of broken refs.
        paths: list[str] = []
        cid_to_path: dict[str, str] = {}
        for att in attachment_dicts:
            if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
                continue
            name = att.get("name", "attachment")
            raw_b64 = att.get("contentBytes", "")
            if not raw_b64:
                continue
            content_bytes = base64.b64decode(raw_b64)
            if not content_bytes:
                continue
            path = self.save_attachment(name, content_bytes)
            paths.append(path)
            content_id = (att.get("contentId") or "").strip()
            if content_id:
                cid_to_path[content_id] = path

        if cid_to_path and body_text:
            body_text = self._rewrite_cid_refs(body_text, cid_to_path)

        description = f"Subject: {subject}\n\n{body_text}" if subject else body_text

        title = await smart_title(description) or subject
        task_type = auto_classify_type(title, description)

        return {
            "title": title,
            "description": description,
            "task_type": task_type.value,
            "attachments": paths,
            "message_id": effective_id,
        }

    @staticmethod
    def _rewrite_cid_refs(body: str, cid_to_path: dict[str, str]) -> str:
        """Replace ``cid:<contentId>`` markdown image refs in *body* with the
        permanent ``/uploads/<basename>`` paths we just saved. Outlook surrounds
        contentIds with angle brackets in the email source but Graph reports them
        without — handle both forms defensively."""
        from urllib.parse import quote as _quote

        def _resolve(cid: str) -> str | None:
            cid = cid.strip().strip("<>").strip()
            if not cid:
                return None
            return cid_to_path.get(cid)

        # Markdown image refs: ![alt](cid:foo)
        def _img_sub(match: re.Match[str]) -> str:
            alt = match.group(1)
            cid = match.group(2)
            path = _resolve(cid)
            if not path:
                return match.group(0)
            return f"![{alt}](/uploads/{_quote(Path(path).name)})"

        body = re.sub(r"!\[([^\]]*)\]\(cid:([^)\s]+)\)", _img_sub, body)

        # Plain link/text refs: [text](cid:foo) or bare cid:foo URLs
        def _link_sub(match: re.Match[str]) -> str:
            text = match.group(1)
            cid = match.group(2)
            path = _resolve(cid)
            if not path:
                return match.group(0)
            return f"[{text}](/uploads/{_quote(Path(path).name)})"

        body = re.sub(r"\[([^\]]+)\]\(cid:([^)\s]+)\)", _link_sub, body)
        return body

    def _notify_draft_failed(self, task_title: str, task_id: str, error: str) -> None:
        """Broadcast draft-reply failure to WS clients and log it."""
        self._broadcast_ws(
            {
                "type": "draft_reply_failed",
                "task_title": task_title,
                "task_id": task_id,
                "error": error,
            }
        )
        self._drone_log.add(
            SystemAction.DRAFT_FAILED,
            "system",
            f"{task_title[:_TITLE_PREVIEW_LEN]}: {error[:_ERROR_PREVIEW_LEN]}"
            if error
            else task_title[:_TITLE_NOTIFY_LEN],
            category=LogCategory.SYSTEM,
            is_notification=True,
        )

    async def send_completion_reply(
        self,
        message_id: str,
        task_title: str,
        task_type: str,
        resolution: str,
        task_id: str = "",
    ) -> None:
        """Draft a reply via Queen and create as draft in Outlook."""
        try:
            # Resolve RFC 822 Message-ID (<...@...>) to Graph message ID
            graph_id = message_id
            if "<" in message_id and "@" in message_id:
                resolved = await self._graph_mgr.resolve_message_id(message_id)
                if not resolved:
                    reason = f"Could not resolve RFC 822 ID '{message_id[:_MSG_ID_PREVIEW_LEN]}'"
                    _log.warning(reason)
                    self._notify_draft_failed(task_title, task_id, reason)
                    return
                graph_id = resolved

            reply_text = await self._queen.draft_email_reply(task_title, task_type, resolution)
            reply_html = _format_reply_html(reply_text)
            ok = await self._graph_mgr.create_reply_draft(graph_id, reply_html)
            if ok:
                _log.info("Draft reply created for task '%s'", task_title[:_TITLE_LOG_LEN])
                self._broadcast_ws({"type": "draft_reply_ok", "task_title": task_title})
                self._drone_log.add(
                    SystemAction.DRAFT_OK,
                    "system",
                    task_title[:_TITLE_NOTIFY_LEN],
                    category=LogCategory.SYSTEM,
                )
            else:
                _log.warning("Draft reply failed for task '%s'", task_title[:_TITLE_LOG_LEN])
                self._notify_draft_failed(task_title, task_id, "Graph API returned failure")
        except Exception as exc:  # broad catch: Graph/Queen errors are unpredictable
            _log.warning("Draft reply error for '%s'", task_title[:_TITLE_LOG_LEN], exc_info=True)
            self._notify_draft_failed(task_title, task_id, str(exc)[:_ERROR_LOG_LEN])
