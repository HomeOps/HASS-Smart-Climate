# Copilot Instructions

## Release process

This repository uses **release-please** (`.github/workflows/release-please.yml`) to
automate releases. Releases are driven by **conventional-commit prefixes** in PR titles
and commit messages.

| Commit prefix | Version bump | Release-notes section |
|---|---|---|
| `feat:` / `feat(scope):` | Minor | Features |
| `fix:` / `fix(scope):` | Patch | Bug Fixes |
| `docs:` | Patch | Documentation |
| `chore:` / `ci:` / `build:` / `refactor:` | Patch | (hidden by default) |
| `feat!:` or any `!` suffix | Major | ⚠ Breaking Changes |

**Always use the appropriate conventional-commit prefix in your PR title.**
No manual labels are required for the release process.

### How a release is made

1. Every push to `main` triggers the **release-please** workflow.
2. release-please opens (or updates) a **Release PR** that:
   - Bumps the version in `custom_components/smart_climate/manifest.json`.
   - Updates `CHANGELOG.md`.
3. A maintainer merges the Release PR.
4. release-please creates the GitHub Release and git tag pointing at the commit
   that already contains the correct `manifest.json` version — no post-release
   fixup needed.

> **Note:** Do not manually edit `manifest.json` version or `CHANGELOG.md`.
> release-please manages both automatically via the Release PR.

## Testing

Run the test suite with:

```bash
pip install -r requirements_test.txt
pytest
```

## Build / lint

There is no separate build step. Linting is handled by the CI workflow (`.github/workflows/ci.yml`).
