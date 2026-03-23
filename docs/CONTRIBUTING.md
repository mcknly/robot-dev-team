<!--
Robot Dev Team Project
File: docs/CONTRIBUTING.md
Description: Contribution guidelines for the Robot Dev Team Project.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
-->

# Contributing Guidelines

Thank you for contributing to the Robot Dev Team automation service. This guide outlines expectations for issues, merge requests, and review cadence.

## Working on Issues

- **Plan first** — review the related GitLab issue and confirm open questions before writing code.
- **Branch naming** — use `issue-<number>-<short-description>` (e.g., `issue-9-doc-refresh`).
- **Atomic commits** — group logically-related changes and follow the instructions in `AGENTS.md` for commit preparation.

## Pull Request Expectations

1. Ensure your branch is rebased on the latest `main`.
2. Run the full test suite (`uv run pytest`) and lint checks before opening the MR.
3. Fill in the MR template with:
   - Summary of changes
   - Testing evidence
   - Deployment considerations
4. Keep the MR focused—create follow-up issues for unrelated cleanups.

## Code and Documentation Standards

- Follow formatting and logging guidance from `AGENTS.md`.
- Keep documentation ASCII-only unless updating existing Unicode content.
- Update or create docs under `docs/` instead of the repository root (except `README.md` and `AGENTS.md`).
- When adding features, update:
  - `docs/CHANGELOG.md`
  - Relevant guides (routing, environment, onboarding, dashboard, etc.)

### File Headers & Licensing

- Every tracked source file, script, and Markdown/YAML document must begin with the standard header:
  - Block-comment languages (Python):
    ```python
    """Robot Dev Team Project
    File: path/to/file.py
    Description: One-line summary.
    License: MIT
    SPDX-License-Identifier: MIT
    Copyright (c) 2025 MCKNLY LLC
    """
    ```
  - Line-comment languages (shell, YAML, Dockerfiles):
    ```bash
    # Robot Dev Team Project
    # File: scripts/example.sh
    # Description: One-line summary.
    # License: MIT
    # SPDX-License-Identifier: MIT
    # Copyright (c) 2025 MCKNLY LLC
    ```
- Markdown and other comment-less formats use an HTML comment wrapper.
- Use the actual repository-relative path and describe the file’s purpose succinctly.
- Validate headers (and detect missing lines) with `uv run python scripts/header_guard.py`; CI should treat failures as blockers.
- Extend the checker by creating `config/header_guard.toml` (see `config/header_guard.toml.example`) to add extra suffixes, filenames, or exclusion prefixes without editing the script.
- The project is licensed under MIT—include the header when adding new files and update the dependency license table in `README.md` if you introduce third-party packages.

## Testing Requirements

- Add targeted tests for new behaviours or bug fixes.
- Mock external tools (`glab`, agent CLIs) to keep tests deterministic.
- Document known gaps or skipped tests in the MR description.

## Review Checklist

Before requesting review, verify:

- [ ] You ran formatting and linting suites.
- [ ] You updated documentation where applicable.
- [ ] You provided reproduction steps or sample payloads if relevant.
- [ ] You noted any follow-up work in linked issues.

Happy automating!
