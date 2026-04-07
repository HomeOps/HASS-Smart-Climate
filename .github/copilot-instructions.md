# Copilot Instructions

## Release process

This repository uses **release-drafter** (`.github/release-drafter.yml`) to automate
release notes. Releases are **not** driven by conventional-commit prefixes
(e.g. `feat:` / `fix:`). Instead, the correct label must be applied to every PR so
that release-drafter places it in the right section:

| Label(s) | Release-notes section |
|---|---|
| `feature`, `enhancement` | 🚀 Features |
| `fix`, `bugfix`, `bug` | 🐛 Bug Fixes |
| `test`, `tests` | 🧪 Tests |
| `documentation`, `docs` | 📖 Documentation |
| `chore`, `maintenance`, `dependencies` | 🧰 Maintenance |

Version bump rules (also controlled by labels):

| Label(s) | Version bump |
|---|---|
| `major`, `breaking-change` | Major |
| `minor`, `feature`, `enhancement` | Minor |
| `patch`, `fix`, `bugfix`, `bug`, `documentation`, `chore`, `maintenance`, `dependencies` | Patch |

**When opening a PR, always apply the appropriate label(s) listed above.**

### How a release is made

1. **release-drafter** keeps a draft GitHub Release updated as PRs are merged.
2. A maintainer publishes the draft release (which creates a git tag).
3. The **Release** workflow (`.github/workflows/release.yml`) triggers on the
   `release: published` event, updates `custom_components/smart_climate/manifest.json`
   with the version from the tag, and commits the change to `main`.

> **Note:** You do not need to manually update `manifest.json` before a release.
> The workflow handles it automatically.

## Testing

Run the test suite with:

```bash
pip install -r requirements_test.txt
pytest
```

## Build / lint

There is no separate build step. Linting is handled by the CI workflow (`.github/workflows/ci.yml`).
