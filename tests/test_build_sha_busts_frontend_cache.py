"""The cache-buster on /static/dashboard.js was keyed on Python source only.

HOW IT PRESENTED. A Queen composer was added to dashboard.html + dashboard.js. The field
rendered — Jinja reads templates per request — and neither Enter nor its Send button did
anything. Nothing was wrong with the feature: `build_sha()` feeds
`/static/dashboard.js?v={{ build_sha }}`, `_hash_source_tree()` hashed only `*.py`, so a
front-end-only change produced a BYTE-IDENTICAL `?v=` and the browser never re-fetched.
The page was running last build's script against this build's markup.

A DAEMON RESTART DOES NOT FIX IT. `_BUILD_SHA` is process-cached, so a restart recomputes
— to the same value, because no `.py` changed. The URL is identical either way, and only a
hard refresh clears it. That is what makes this worth a test rather than a note.

MEASURED BEFORE FIXING: editing dashboard.js left the hash at 91dd934e, unchanged; editing
update.py moved it to 092d85d5. The second is the positive control — without it, "the hash
did not change" could equally have meant the hashing was broken outright.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from swarm.update import _BUILD_HASH_SUFFIXES, _hash_source_tree

_SRC = Path(__file__).resolve().parent.parent / "src" / "swarm"


@pytest.fixture
def restore():
    """Edit a real source file and put it back, whatever happens."""
    saved: list[tuple[Path, bytes]] = []

    def _touch(rel: str) -> None:
        p = _SRC / rel
        saved.append((p, p.read_bytes()))
        p.write_bytes(p.read_bytes() + b"\n/* build-hash probe */\n")

    yield _touch
    for p, data in reversed(saved):
        p.write_bytes(data)


@pytest.mark.parametrize(
    "rel",
    [
        "web/static/dashboard.js",
        "web/templates/base.html",
        "web/templates/dashboard.html",
    ],
)
def test_a_frontend_change_changes_the_build_hash(restore, rel):
    """THE BUG, DIRECTLY. Each of these is served to the browser under a `?v=` derived
    from this hash; if the hash does not move, the browser does not re-fetch."""
    before = _hash_source_tree()

    restore(rel)

    assert _hash_source_tree() != before, (
        f"editing {rel} left the build hash unchanged — /static/dashboard.js?v= is "
        f"byte-identical, so the browser serves its cached copy and the change never "
        f"reaches the page"
    )


def test_a_python_change_still_changes_it(restore):
    """POSITIVE CONTROL. Without this, the tests above would also pass against a hash
    function that returned a constant."""
    before = _hash_source_tree()

    restore("update.py")

    assert _hash_source_tree() != before


def test_the_hash_is_stable_when_nothing_changes():
    """NEGATIVE CONTROL. A hash that moved on every call would pass every test above and
    bust the cache on every single page load — turning one stale-asset bug into a
    permanent re-download of the whole front end."""
    assert _hash_source_tree() == _hash_source_tree()


def test_the_web_asset_types_are_all_covered():
    """Named rather than inferred: .js/.css/.html are the three things the browser caches
    under a build-stamped URL."""
    for suffix in (".py", ".js", ".css", ".html"):
        assert suffix in _BUILD_HASH_SUFFIXES


def test_a_rename_alone_changes_the_hash(restore, tmp_path):
    """Contents-only hashing calls a rename the same build, but the browser must fetch a
    different URL. Cheap to include, and the failure is invisible without it."""
    a = _SRC / "web" / "static" / "_probe_a.js"
    b = _SRC / "web" / "static" / "_probe_b.js"
    a.write_bytes(b"// probe\n")
    try:
        first = _hash_source_tree()
        a.rename(b)
        assert _hash_source_tree() != first
    finally:
        for p in (a, b):
            if p.exists():
                p.unlink()


def test_it_does_not_read_the_whole_tree_blindly():
    """Hashing every file would pull in __pycache__ and any stray artefact, making the
    hash unstable for reasons that have nothing to do with the build."""
    junk = _SRC / "web" / "static" / "_probe.pyc"
    junk.write_bytes(b"binary junk")
    try:
        assert _hash_source_tree() == _hash_source_tree()
        h = hashlib.sha256()
        for path in sorted(_SRC.rglob("*")):
            if path.suffix in _BUILD_HASH_SUFFIXES and path.is_file():
                h.update(str(path.relative_to(_SRC)).encode())
                h.update(path.read_bytes())
        assert _hash_source_tree() == h.hexdigest()[:8], ".pyc leaked into the build hash"
    finally:
        junk.unlink()
