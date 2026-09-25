<!--
Robot Dev Team Project
File: .github/PULL_REQUEST_TEMPLATE.md
Description: Pull request template for the public GitHub mirror.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
-->

<!--
Before you fill this in, two things that are easy to get wrong here:

  1. This pull request must target `rc`, not `main`. Public `main` is written only by release
     automation, one commit per release, and a pull request against it cannot be merged.
  2. No CI runs on this pull request. GitHub Actions carry no build, qualification, or release
     authority here. The checks below are yours to run locally.

docs/CONTRIBUTING.md explains the full flow; docs/MIRRORING.md explains why it works this way.
-->

## Summary

<!-- What changes, and why. Link the GitHub issue if there is one. -->

## Testing evidence

<!-- What you ran and what it said. Paste the relevant output rather than asserting it passed. -->

## Deployment considerations

<!-- Config changes, migration steps, new environment variables, or "none". -->

## Checklist

- [ ] This pull request targets `rc`.
- [ ] Every commit is signed off (`git commit -s`, adding a `Signed-off-by:` line), certifying
      the Developer Certificate of Origin 1.1 at https://developercertificate.org/.
- [ ] `pytest` passes.
- [ ] `ruff check --no-cache --no-fix .` passes.
- [ ] `mypy app` passes.
- [ ] `python scripts/header_guard.py` passes -- every new tracked file carries the license header.
- [ ] Documentation under `docs/` is updated where behaviour changed.
- [ ] `docs/CHANGELOG.md` has an entry under `[Unreleased]` if this is user-visible.
- [ ] This is not a security vulnerability report (those go through `SECURITY.md`, privately).

<!--
What happens next: accepted work is re-applied onto canonical `main` and qualified there before
this pull request is merged into `rc`, with a merge commit. Your commits stay reachable under your
own authorship -- `rc` becomes a second parent of the next release commit. Your change becomes
public with the next stable release, not on merge.
-->
