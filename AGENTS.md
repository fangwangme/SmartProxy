## Development Environment

- Python: `source .venv/bin/activate`
- Node.js: `cd dashboard && bun install`

### Creating the Python `.venv`

`.venv/` is git-ignored and **per checkout** — a worktree does not inherit the
main checkout's environment, and a venv copied or moved between paths breaks
(its `bin/python` symlink and the script shebangs hardcode the original absolute
path). Create one in each checkout you work in:

```bash
uv venv --python 3.12 .venv
uv pip install -r requirements.txt --python .venv/bin/python
```

Run tools through `.venv/bin/...` (e.g. `.venv/bin/python -m pytest tests/ -q`)
rather than relying on an activated shell, so the right interpreter is used
regardless of which checkout the shell was activated in.

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
