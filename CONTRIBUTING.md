# Contributing

Thanks for taking a look.

## Dev setup

```bash
uv venv && uv pip install -e ".[dev]"     # or: python -m venv .venv && pip install -e ".[dev]"
pytest -m "not integration"
ruff check .
```

`-m "not integration"` skips the tests that actually spawn the `claude` / `codex` CLIs.
The integration tests need both CLIs installed and logged in.

## Layout

- `src/cc_bridge/bridge/` — the bridge itself (runs the other agent's CLI as a subprocess)
- `src/cc_bridge/installer/` — env detection, config writing, GUI installer
- `tests/` — keep `pytest -m "not integration"` and `ruff` green

Issues and PRs welcome.
