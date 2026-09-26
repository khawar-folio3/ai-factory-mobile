# Contributing

Thanks for helping. Small, focused PRs get reviewed fastest.

## Setup

```bash
git clone https://github.com/khawar-folio3/ai-factory-mobile && cd ai-factory-mobile
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
make check        # ruff, format check, mypy --strict, pytest with coverage
```

## Ground rules

- **The runner decides, the agent thinks.** Anything that must always happen (a gate, a limit, a refusal) belongs in
  Python with a test, not only in a skill's prose.
- Every new behaviour ships with a test. Pipeline behaviour is tested end to end in `tests/test_pipeline.py` against a
  temporary git repo and a `FakePlatform`; follow that pattern.
- No network in tests. Stub `gh`, trackers and devices.
- Keep dependencies minimal (currently: typer, pydantic, pyyaml). Discuss new ones in an issue first.
- Skills (`src/mobile_factory/skills/*.md`) are product surface: keep them short, imperative, and tool-agnostic
  (no Claude- or Cursor-only instructions).
- Event types and output schemas are public contracts. Additive changes only within a minor version; note breaking
  changes in `CHANGELOG.md`.

## Adding a detector

Add an entry to `src/mobile_factory/templates/slop.yaml` with a new `S0xx` id, a test in `tests/test_guardrail.py`
showing one true positive and one near-miss that must not match, and keep false positives rare: a detector that cries
wolf trains reviewers to dismiss it.

## Commit and PR

- Conventional, imperative subject (`Add iOS screen state`, `Fix rollback of new files`).
- Describe the behaviour change and how you tested it.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
