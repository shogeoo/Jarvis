# Contributing

Use a virtual environment and install `python -m pip install -e '.[dev]'`.
Run `ruff check jarvis tests` and
`python -m unittest discover -s tests -p 'test_*.py' -v` before submitting changes.
Build artifacts with `python -m build`; package resources must also work when
installed from a wheel outside the checkout.

Tests use temporary storage and synthetic SDK fixtures. They must not read or
change a contributor's live storage, contact a model API, or control devices.
Do not commit contexts, credentials, logs, weights, or personal extensions.

Follow GitFlow: features branch from `develop` into `feature/*` and merge back
into `develop`. Releases use `release/*`; production fixes use `hotfix/*`.
Use Conventional Commits: `type(scope): description`.

Keep changes scoped to their contract. Retain restore, caller-directed errors,
context-wide call IDs and exactly-once results. Do not add action result timeouts
or implicit context truncation. Document changes to public SDK and protocol.
