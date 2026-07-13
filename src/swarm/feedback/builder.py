"""Assemble the final markdown body and GitHub issue URL.

Takes a user-edited :class:`FeedbackPayload` (title, description, category,
attachments) and produces:

- a full markdown report (always available via the "Copy as Markdown"
  fallback), and
- a ``https://github.com/<repo>/issues/new?...`` URL ready to open in a
  new tab, truncated to stay under GitHub's ~8KB URL limit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import quote, urlencode

# Hard ceiling for the generated GitHub URL. GitHub's practical limit is
# around 8KB; we stay well below to leave headroom for the base URL +
# encoded title + other params.
_MAX_URL_BYTES = 7500

Category = Literal["bug", "feature", "question"]

_CATEGORY_LABELS: dict[Category, str] = {
    "bug": "bug",
    "feature": "enhancement",
    "question": "question",
}

_DEFAULT_REPO = "miopea/swarm"


@dataclass
class Attachment:
    """Attachment section as it arrives from the frontend.

    This is the frontend-facing shape (plain strings) — distinct from the
    collector's ``Attachment`` to avoid coupling builder to collector.
    """

    key: str
    label: str
    content: str
    enabled: bool = True


@dataclass
class FeedbackPayload:
    """Full report payload ready to be rendered."""

    title: str
    description: str
    category: Category = "bug"
    attachments: list[Attachment] = field(default_factory=list)

    def label(self) -> str:
        return _CATEGORY_LABELS.get(self.category, "bug")


def _render_attachment(att: Attachment) -> str:
    """Render one attachment as a collapsed ``<details>`` block."""
    content = att.content.rstrip() or "(empty)"
    # Use a fenced code block inside the details so log lines don't get
    # interpreted as markdown.
    return f"<details><summary>{att.label}</summary>\n\n```\n{content}\n```\n\n</details>"


def build_markdown(payload: FeedbackPayload) -> str:
    """Render the full markdown body (no truncation)."""
    parts: list[str] = []

    desc = payload.description.strip() or "_(no description provided)_"
    parts.append("## Description\n\n" + desc)

    enabled = [a for a in payload.attachments if a.enabled and a.content.strip()]
    if enabled:
        parts.append("## Diagnostics")
        parts.extend(_render_attachment(a) for a in enabled)

    return "\n\n".join(parts) + "\n"


def _build_url(repo: str, title: str, body: str, label: str) -> str:
    params = {
        "title": title,
        "body": body,
        "labels": label,
    }
    return f"https://github.com/{repo}/issues/new?" + urlencode(params, quote_via=quote)


def build_issue_url(
    payload: FeedbackPayload,
    *,
    repo: str = _DEFAULT_REPO,
) -> tuple[str, str, bool]:
    """Build the GitHub issue URL for *payload*.

    Returns ``(url, full_markdown, truncated)``.

    If the encoded URL would exceed :data:`_MAX_URL_BYTES`, the largest
    attachment is progressively shrunk (oldest lines dropped first) until
    the URL fits, and a trailing ``[truncated]`` notice is appended to the
    shrunken section. ``truncated`` indicates whether this happened — the
    UI should show a "Copy as Markdown" hint in that case.
    """
    full_markdown = build_markdown(payload)
    label = payload.label()

    url = _build_url(repo, payload.title, full_markdown, label)
    if len(url) <= _MAX_URL_BYTES:
        return url, full_markdown, False

    # --- Truncation path ---------------------------------------------------
    # Strategy: iteratively shrink the longest enabled attachment by
    # dropping leading lines until the URL fits. This keeps the most recent
    # log lines, which are the most relevant for debugging.
    truncation_notice = "\n... [truncated — use 'Copy as Markdown' for the full report]"

    # Work on a mutable copy of attachments
    atts = [
        Attachment(
            key=a.key,
            label=a.label,
            content=a.content,
            enabled=a.enabled,
        )
        for a in payload.attachments
    ]
    working = FeedbackPayload(
        title=payload.title,
        description=payload.description,
        category=payload.category,
        attachments=atts,
    )

    max_iterations = 50  # safety cap so we can't loop forever
    for _ in range(max_iterations):
        # Find the longest enabled attachment
        candidates = [a for a in working.attachments if a.enabled and len(a.content) > 100]
        if not candidates:
            break
        victim = max(candidates, key=lambda a: len(a.content))

        # Drop the first 25% of its lines
        lines = victim.content.splitlines()
        if len(lines) <= 4:
            # Too small to shrink further — just disable it
            victim.enabled = False
        else:
            keep = lines[len(lines) // 4 :]
            victim.content = "\n".join(keep) + truncation_notice

        candidate_md = build_markdown(working)
        candidate_url = _build_url(repo, working.title, candidate_md, label)
        if len(candidate_url) <= _MAX_URL_BYTES:
            return candidate_url, full_markdown, True

    # Last resort: drop all attachments, keep title + description
    working.attachments = []
    minimal_md = build_markdown(working) + (
        "\n\n_(Attachments omitted — URL length limit. "
        "Use 'Copy as Markdown' to get the full report.)_\n"
    )
    minimal_url = _build_url(repo, working.title, minimal_md, label)
    return minimal_url, full_markdown, True
