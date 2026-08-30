## Development Environment

- Python: **3.14**, managed by **uv**. `uv sync --locked` then run tools through `.venv/bin/...`
- Node.js: `cd dashboard && bun install`

### Creating the Python `.venv`

`.venv/` is git-ignored and **per checkout** — a worktree does not inherit the
main checkout's environment, and a venv copied or moved between paths breaks
(its `bin/python` symlink and the script shebangs hardcode the original absolute
path). Create one in each checkout you work in:

```bash
uv sync --locked
```

That reads `pyproject.toml` + `uv.lock` and creates `.venv` on the interpreter
pinned in `.python-version`, installing uv's own 3.14 build if the machine has
none. `--locked` makes the command fail if `uv.lock` would need to change.

**Without uv**, the pip path still works:

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`requirements.txt` is **generated** — it is the fully-pinned pip-compatible
export of the lockfile, kept only so a machine without uv can install. Never
hand-edit it. Dependencies are declared in `pyproject.toml`; after changing them
run:

```bash
uv lock
uv export --frozen --no-hashes --no-emit-project -o requirements.txt
```

Run tools through `.venv/bin/...` (e.g. `.venv/bin/python -m pytest tests/ -q`)
rather than relying on an activated shell, or on a bare `python3` — that resolves
to the system interpreter, not the project's, and it is the wrong one.

If an existing `.venv` misbehaves, check whether it is stale before debugging
anything else — `ls -l .venv/bin/python` pointing at a non-existent interpreter,
or `head -1 .venv/bin/pytest` showing a path from a different checkout, both mean
the venv should be deleted and recreated with the commands above.

## Testing

```bash
.venv/bin/python -m pytest tests/ -q
```

Tests construct `ProxyManager` from a **real `.ini` file** written to a temp
directory (see `write_config_file` in `tests/test_smart_proxy.py`). Do not patch
`configparser.ConfigParser.read` to inject config: `read()`'s return value is
never used by configparser, so the patch leaves `manager.config` empty and every
setting silently falls back to its hardcoded default — the test then passes
without ever exercising the configured value.

## Project Structure

- Development is done directly on the `main` branch
- Local state and build outputs live under `.local/`
- Shared specs live under `docs/specs/`
- Agent notes, plans, archives, and project status live under `.agents/` (local-only, git-ignored)
