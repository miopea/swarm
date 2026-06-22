# Contributing to Swarm

Thanks for your interest in Swarm. This is a Python 3.12+ project managed with
[`uv`](https://docs.astral.sh/uv/).

## Development setup

```bash
git clone https://github.com/miopea/swarm.git
cd swarm
uv sync                    # install dependencies into .venv
uv run swarm --help        # run the CLI from source
```

`uv run swarm ...` runs the **dev** version from the source tree. The installed
binary on your PATH (from `uv tool install`) is separate — see the README's
"Dev vs Installed Version" notes if you're iterating on the running daemon.

## Before you open a PR

Run the full local gate and make sure it's green with **zero warnings**:

```bash
uv run ruff format src/ tests/    # format
uv run ruff check src/ tests/     # lint (must be clean)
uv run pytest tests/ -q           # full test suite (must pass)
```

A test failure or a lint warning is treated as a blocking error, not a
suggestion. Fix it before pushing.

## Code conventions

- **Explicit types.** No bare `any`; don't paper over type errors with
  `# type: ignore` — fix the underlying issue.
- **Tests first.** New behaviour and bug fixes ship with tests. Bug fixes
  start with a failing regression test that the fix turns green.
- **Search before you add.** Reuse existing utilities and patterns; the code
  you need often already exists.
- **Minimal, focused changes.** Don't refactor adjacent code in a bug-fix PR.
- Async everywhere for I/O; feature-based modules under `src/swarm/`
  (`worker/`, `drones/`, `queen/`, `tasks/`, `mcp/`, `server/`, …). See the
  README "Architecture" section for the layout.

## Commits & releases

- Conventional-commit summaries (`fix:`, `feat:`, `docs:`, …).
- Swarm uses **calendar versioning** (`YYYY.M.D[.N]`). The release helper at
  `scripts/release.py` bumps the version across `pyproject.toml` and
  `src/swarm/__init__.py` and promotes `CHANGELOG.md`'s `## Unreleased`
  section to a dated entry. Release commits use a `release: X.Y.Z` summary.
- Add a `CHANGELOG.md` entry under `## Unreleased` for any user-facing change.

## Reporting bugs / requesting features

Open an issue on [GitHub](https://github.com/miopea/swarm/issues) with steps to
reproduce (for bugs) or the use case you're after (for features).

## License

By contributing, you agree your contributions are licensed under the project's
[MIT License](LICENSE).
