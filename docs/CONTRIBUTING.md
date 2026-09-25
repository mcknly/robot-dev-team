<!--
Robot Dev Team Project
File: docs/CONTRIBUTING.md
Description: Contribution guidelines for the Robot Dev Team Project.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
-->

# Contributing Guidelines

Thank you for contributing to the Robot Dev Team automation service. This guide outlines expectations for issues, changes, and review cadence.

## Where to Contribute

There are two contribution paths, and which one applies depends on whether you have access to the
canonical instance. Development happens on a private self-hosted GitLab instance. On the public
GitHub repository the **release line** — `main`, the stable tags, and the release artifacts — is a
read-only projection of that work, written by release automation and by nothing else. **Intake** is
writable: Issues, pull requests, and the `rc` branch are where contributions arrive.
[`MIRRORING.md`](MIRRORING.md) explains that model in full.

| | Canonical path | External path |
| --- | --- | --- |
| Who | Maintainers and agents with access to the private GitLab instance | Everyone else |
| Intake | GitLab issues | GitHub issues on `mcknly/robot-dev-team` |
| Changes | GitLab merge requests targeting `main` | GitHub pull requests targeting **`rc`** |
| CI on your change | Full pipeline | None — run the gates locally |
| Section | [Canonical Contributions](#canonical-contributions) | [External Contributions](#external-contributions) |

Security vulnerabilities do not go through either path. See [`../SECURITY.md`](../SECURITY.md).

## External Contributions

Contributions from outside the canonical instance are welcome, through the public GitHub
repository. The flow is not the usual one, so it is worth reading before you open anything.

1. **Open an issue first for anything non-trivial.** Public GitHub issues are the intake point. A
   typo fix or an obviously-correct bug fix can go straight to a pull request; a feature or a
   behaviour change is better discussed before you write it, because the review that decides it
   happens on the canonical instance and you cannot see that discussion.
2. **Target `rc`, never `main`.** Public `main` is written only by release automation, one commit
   per release. A pull request against `main` cannot be merged, because merging it would put an
   unqualified commit into the published release line. GitHub will offer `main` as the base anyway
   — it bases a pull request on the repository's default branch, and no setting changes that while
   keeping `main` as the clone default — so **set the base branch to `rc` yourself**, or start from
   a compare URL that already has it:
   `https://github.com/mcknly/robot-dev-team/compare/rc...YOUR-FORK:YOUR-BRANCH`. If one arrives
   against `main` a maintainer will retarget it or ask you to reopen it; nothing is lost either way.
3. **Sign off your commits.** Every commit needs a Developer Certificate of Origin sign-off —
   `git commit -s`, which appends a `Signed-off-by: Your Name <your@email>` line. That line
   certifies the statements in [Developer Certificate of Origin 1.1](https://developercertificate.org/);
   please read it rather than a paraphrase, since it is what you are certifying. No automated check
   enforces this, because no CI runs on the pull request — a maintainer will not replay a commit
   that lacks the trailer, so a missing sign-off costs you a round trip.
4. **Run the gates locally.** No CI runs on your pull request. GitHub Actions carry no build,
   qualification, or release authority here, and that is deliberate — a second pipeline would be a
   second opinion on whether a change is fit to release. So the checks in
   [Change Expectations](#change-expectations) below are yours to run: `pytest`, `ruff`, `mypy`, and
   `python scripts/header_guard.py`. The header guard in particular rejects any new tracked file
   without the standard license header, and it is the check external contributions most often miss.
5. **Expect a replay, not a merge button.** Accepted work is re-applied onto canonical `main` and
   reviewed and qualified there first. Only after it passes does a maintainer merge your pull
   request into `rc`. The order is what keeps a rejected change out of the permanent public record:
   once `rc` is a parent of a release commit, your commit is in the published ancestry for good, and
   nothing removes it afterwards without a history rewrite.
6. **Your authorship survives this.** `rc` is included as a second parent of the next release
   commit, so your own commits — with your name on them — stay reachable in the public history
   rather than disappearing into a maintainer's synthetic commit. To be exact about what that
   guarantees: the commits are reachable with their authorship intact, and your pull request is
   merged with a merge commit precisely so that stays true. The release commit's tree is still the
   canonical snapshot, so a first-parent walk and `git blame` attribute the released lines to it.

Two things this means in practice: your change becomes public when the **next stable release** is
published, not when it is approved; and review feedback may arrive without the reasoning behind it
being visible to you, because the deciding discussion is on the private instance. Ask if something
seems arbitrary — it usually has a reason that just is not in front of you.

Nothing is ever synced from GitHub back into the canonical instance automatically. A maintainer
applies it by hand, on purpose.

## Canonical Contributions

- **Plan first** — review the related GitLab issue and confirm open questions before writing code.
- **Branch naming** — use `issue-<number>-<short-description>` (e.g., `issue-9-doc-refresh`).
- **Atomic commits** — group logically-related changes and follow the instructions in `AGENTS.md` for commit preparation.

## Change Expectations

These apply to both paths — a GitLab merge request and a GitHub pull request are held to the same
standard, because the external change is qualified on the canonical instance before it is accepted.

1. Ensure your branch is rebased on the latest `main` (external path: on `rc`).
2. Run the full test suite (`uv run pytest`) and the lint/type checks before opening the change.
   These are the gates CI enforces on the canonical instance, so run them exactly as CI does:
   - `ruff check --no-cache --no-fix .` — `ruff.toml` sets `fix = true`, so pass `--no-fix`
     to *validate* rather than silently rewrite.
   - `mypy app` — point `MYPY_CACHE_DIR` at a writable path when the checkout is a
     read-only mount, or mypy crashes writing `.mypy_cache/`.
   - `python scripts/header_guard.py` — confirms the license headers.
3. Fill in the MR or PR template with:
   - Summary of changes
   - Testing evidence
   - Deployment considerations
4. Keep the change focused—create follow-up issues for unrelated cleanups.

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
- [ ] External path only: the pull request targets `rc`, and every commit carries a DCO `Signed-off-by:` line.

Happy automating!
