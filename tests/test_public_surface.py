"""Robot Dev Team Project
File: tests/test_public_surface.py
Description: Repository-state checks for the public source surface a stranger reaches.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
GITHUB_DIR = REPO_ROOT / ".github"
ISSUE_TEMPLATE_DIR = GITHUB_DIR / "ISSUE_TEMPLATE"
PULL_REQUEST_TEMPLATE = GITHUB_DIR / "PULL_REQUEST_TEMPLATE.md"
CHOOSER_CONFIG = ISSUE_TEMPLATE_DIR / "config.yml"
README = REPO_ROOT / "README.md"
SECURITY = REPO_ROOT / "SECURITY.md"
MIRRORING = REPO_ROOT / "docs" / "MIRRORING.md"
CONTRIBUTING = REPO_ROOT / "docs" / "CONTRIBUTING.md"
CHANGELOG = REPO_ROOT / "docs" / "CHANGELOG.md"
TESTS_DIR = Path(__file__).resolve().parent

PUBLIC_REPO = "https://github.com/mcknly/robot-dev-team"
# The only published reporting channel. `SECURITY.md` previously pointed at a GitLab confidential
# issue on an instance a stranger cannot reach, which is the failure this file exists to prevent
# from returning: an unusable path reads exactly like a usable one.
PVR_URL = f"{PUBLIC_REPO}/security/advisories/new"
UNREACHABLE_DISCLOSURE = "confidential_issues"
PUBLIC_IMAGE = "docker.io/mcknly/robot-dev-team"
# Contributor intake. `main` is written only by release automation, so a pull request against it
# cannot be merged -- the branch name has to agree everywhere a contributor might read it.
INTAKE_BRANCH = "rc"
# The end of the legacy prefix on the public repository. The mirror policy describes the history
# shape in terms of this commit and the owner checklist creates `rc` at it, so the two have to name
# the same point or the checklist silently drifts off the document that explains it.
LEGACY_PREFIX_COMMIT = "447f674"
DCO_URL = "https://developercertificate.org/"

# The README lead-in. It replaced a full top-level policy section, which was removed deliberately:
# repository governance is secondary to understanding and running the project, and it does not
# belong ahead of Quick Start. The detail now lives in `docs/MIRRORING.md`.
README_NOTE_MARKER = "**Source publication note.**"

# A GitHub file URL into this repository, as used by the issue chooser and the issue forms. These
# are the "one click from the registry page" paths, so a rename that leaves one behind produces a
# 404 for the exact reader this surface exists for.
BLOB_URL = re.compile(rf"{re.escape(PUBLIC_REPO)}/blob/main/(?P<path>[A-Za-z0-9_./-]+)")
# An inline markdown link target. Reference-style links and bare autolinks are deliberately not
# matched: nothing in the public surface uses them, and widening the pattern would pull in prose.
MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\((?P<target>[^)\s]+)\)")
# The canonical instance is referenced by role, never by hostname, anywhere in the published tree.
# It is not reachable from the internet, so a URL serves no reader -- and a literal hands a
# grep-driven scraper a resolvable endpoint, a registry, and an API base already assembled. That is
# the whole benefit and the limit of it: the host is a short guess from the `mcknly` GitHub org or
# the Docker Hub repository, so this is target-list hygiene, not secrecy, and nothing should be
# written that depends on it being secret.
#
# The scope is every tracked file, which is why the needle is assembled at runtime rather than
# written out: the guard must be able to hold at zero contiguous occurrences, including in the file
# that enforces it. The operator runbooks (`docs/CI.md`, `docs/RELEASING.md`) were once exempt as
# "internal" documents; the exemption had no principle behind it, since they are mirrored to the
# same readers as everything else.
#
# This literal is a second, independent spelling of the host that `docs/MIRRORING.md` specifies the
# outbound gate against as `CI_SERVER_HOST`. That is deliberate -- the guard has to work with no CI
# environment and no network -- but the two agree only by hand. A host migration that updates one
# and not the other leaves this check quietly inert while still passing, so move both together.
PRIVATE_HOST = ".".join(("git", "mcknly", "com"))

# Phrasings that call the *whole* public repository read-only. Each of these was in the surface
# once. They are banned rather than merely discouraged because they contradict the workflow the
# same documents send contributors into: `rc`, issues, and pull requests are writable, and a
# blanket "read-only" is wrong in exactly the place a reader is being invited to write. What is
# read-only is the release line -- `main`, the stable tags, and the release artifacts.
UNSCOPED_READ_ONLY_CLAIMS = (
    "read-only mirror",
    "read-only projection of it",
    "the public GitHub repository is a read-only",
    "read-only publication of a private canonical instance",
    "one-way, read-only publication",
    "this repository is read-only",
)

# GitHub issue-form element types, and which of them carry a user-visible field. `markdown` is the
# only type that is not an input, which is why it is the only one exempt from the `id` requirement.
FORM_ELEMENT_TYPES = frozenset({"markdown", "input", "textarea", "dropdown", "checkboxes"})
FORM_INPUT_TYPES = FORM_ELEMENT_TYPES - {"markdown"}

PUBLIC_SURFACE_FILES = (
    MIRRORING,
    SECURITY,
    CONTRIBUTING,
    PULL_REQUEST_TEMPLATE,
    CHOOSER_CONFIG,
)


def issue_form_paths() -> list[Path]:
    """Every issue form, excluding the chooser configuration, which is not a form."""

    return sorted(p for p in ISSUE_TEMPLATE_DIR.glob("*.yml") if p != CHOOSER_CONFIG)


def load_yaml(path: Path) -> object:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def tracked_files() -> frozenset[str] | None:
    """Repository-relative paths of every tracked file, or `None` when git cannot answer.

    Tracking is the property that matters, not existence on this disk. The published tree contains
    tracked files only, and `.gitignore` covers plausible link targets (`.env`, `run-logs/*`,
    `docker-compose.override.yml`, `config/routes.local.yaml`) -- a link to one of those resolves
    for a maintainer with the file present and 404s for every public reader.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return frozenset(entry for entry in result.stdout.decode().split("\0") if entry)


def require_tracked_files() -> frozenset[str]:
    tracked = tracked_files()
    if tracked is None:
        pytest.skip("git is unavailable, so tracked-file membership cannot be checked")
    return tracked


def numbered_steps(section: str) -> list[str]:
    """The enumerated instructions in a markdown section, one entry per step.

    A recipe is checked on what it tells a reader to *do*, not on which words appear anywhere in
    its section. Banning a phrase outright bans it from the prose that has to state the gap too,
    which is how the deferred release manifest came to be missing from the paragraph whose whole
    job is to list what a public reader cannot check.
    """
    steps: list[str] = []
    for line in section.splitlines():
        if re.match(r"^\d+\. ", line):
            steps.append(line)
        elif not line.strip():
            continue
        elif steps and line.startswith("   "):
            steps[-1] += " " + line.strip()
        elif steps:
            # Prose back at the left margin ends the list.
            break
    return [prose(step) for step in steps]


def prose(text: str) -> str:
    """Collapse whitespace so an assertion about a sentence survives a reflow.

    These documents are hard-wrapped, so a phrase that reads as one clause is frequently split
    across a newline. Asserting on the raw bytes makes the guard a formatting check, which fails
    on an innocent rewrap and tempts the next person to weaken the assertion rather than fix it.
    """
    return " ".join(text.split())


def readme_note() -> str:
    """The source publication note, from its lead-in to the end of its paragraph."""

    text = README.read_text(encoding="utf-8")
    assert README_NOTE_MARKER in text, "README carries no source publication note"
    return text.split(README_NOTE_MARKER, 1)[1].split("\n\n", 1)[0]


def security_reporter_section() -> str:
    """The reporter-facing region of `SECURITY.md`: its heading through the next `##`.

    The maintainer path deliberately lives outside this region. Filing a confidential issue on the
    canonical instance is correct advice for someone who can already reach it, and a naive
    substring ban on "confidential issue" would reject that sentence along with the failure it is
    meant to catch.
    """
    text = SECURITY.read_text(encoding="utf-8")
    heading = "## Reporting a Vulnerability"
    assert heading in text
    return text.split(heading, 1)[1].split("\n## ", 1)[0]


def test_issue_templates_are_yaml_forms_not_markdown() -> None:
    """Forms, not legacy `.md` templates, so the license header and GitHub can coexist.

    A legacy markdown template needs its YAML front matter to start at byte 0. The header guard
    requires the license block in the first 12 lines of every tracked markdown file, and an HTML
    comment ahead of the front matter does not fail loudly -- GitHub renders the front matter as
    body text and the template silently stops working.
    """
    forms = issue_form_paths()

    assert forms, "no issue forms found under .github/ISSUE_TEMPLATE/"
    assert not list(ISSUE_TEMPLATE_DIR.glob("*.md"))


@pytest.mark.parametrize("path", issue_form_paths(), ids=lambda p: p.name)
def test_issue_form_is_structurally_valid(path: Path) -> None:
    form = load_yaml(path)

    assert isinstance(form, dict), f"{path.name} is not a mapping"
    for key in ("name", "description", "body"):
        assert form.get(key), f"{path.name} is missing a non-empty {key!r}"

    body = form["body"]
    assert isinstance(body, list) and body, f"{path.name} has an empty body"

    for index, element in enumerate(body):
        where = f"{path.name} body[{index}]"
        assert isinstance(element, dict), f"{where} is not a mapping"

        element_type = element.get("type")
        assert element_type in FORM_ELEMENT_TYPES, f"{where} has unknown type {element_type!r}"

        attributes = element.get("attributes")
        assert isinstance(attributes, dict), f"{where} has no attributes mapping"

        if element_type == "markdown":
            assert attributes.get("value"), f"{where} is an empty markdown block"
            continue

        assert element.get("id"), f"{where} has no id"
        if element_type == "checkboxes":
            options = attributes.get("options")
            assert isinstance(options, list) and options, f"{where} has no checkbox options"
            assert all(option.get("label") for option in options), f"{where} has an unlabelled box"
        else:
            assert attributes.get("label"), f"{where} has no label"

        if element_type == "dropdown":
            assert attributes.get("options"), f"{where} has no dropdown options"


def test_issue_chooser_disables_blank_issues_and_routes_reporters() -> None:
    """Every first contact lands on text that states the policy.

    A blank issue is the one path on which a stranger can reach the maintainers without ever
    being told what this repository is -- including, worst case, with a vulnerability.
    """
    config = load_yaml(CHOOSER_CONFIG)

    assert isinstance(config, dict)
    assert config.get("blank_issues_enabled") is False

    links = config.get("contact_links")
    assert isinstance(links, list) and links

    for link in links:
        assert isinstance(link, dict)
        for key in ("name", "url", "about"):
            assert link.get(key), f"contact link {link!r} is missing {key!r}"

    urls = {link["url"] for link in links}
    assert PVR_URL in urls, "the chooser does not offer the private reporting path"
    assert f"{PUBLIC_REPO}/blob/main/docs/CONTRIBUTING.md" in urls
    assert f"{PUBLIC_REPO}/blob/main/docs/MIRRORING.md" in urls


def test_public_entry_point_links_name_tracked_files() -> None:
    """Absolute links out of `.github/` name tracked files, because nothing else checks them.

    These render on GitHub against the published tree, so a moved or renamed document fails only
    for the external reader -- never for a maintainer, and never in CI, unless it is asserted
    here.
    """
    tracked = require_tracked_files()
    sources = [PULL_REQUEST_TEMPLATE, CHOOSER_CONFIG, *issue_form_paths()]
    checked = 0

    for source in sources:
        for match in BLOB_URL.finditer(source.read_text(encoding="utf-8")):
            target = match.group("path")
            assert target in tracked, f"{source.name} links to untracked {target}"
            checked += 1

    assert checked, "no repository links found in the public entry points"


@pytest.mark.parametrize(
    "path",
    [README, SECURITY, MIRRORING, CONTRIBUTING],
    ids=lambda p: p.name,
)
def test_public_surface_relative_links_resolve(path: Path) -> None:
    """Relative links in the surface name tracked files, and every listed file has some.

    The `checked` count is not decoration. `SECURITY.md` previously referenced the mirror policy
    and the release contract as inline code rather than as links, so its case iterated zero times
    and passed unconditionally while the reporter it pointed at those documents could not click
    through to either.
    """
    tracked = require_tracked_files()
    checked = 0

    for match in MARKDOWN_LINK.finditer(path.read_text(encoding="utf-8")):
        target = match.group("target")
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        resolved = (path.parent / target.split("#", 1)[0]).resolve()
        relative = resolved.relative_to(REPO_ROOT).as_posix()
        assert relative in tracked, f"{path.name} links to untracked {target}"
        checked += 1

    assert checked, f"{path.name} has no relative links, so this case proves nothing"


def test_security_policy_names_a_reachable_reporting_path() -> None:
    text = SECURITY.read_text(encoding="utf-8")

    assert PVR_URL in text
    assert UNREACHABLE_DISCLOSURE not in text, (
        "SECURITY.md points a stranger at a confidential issue on an instance they cannot reach"
    )
    # No address is published, so the document must not imply one exists and leave a reporter
    # hunting for it.
    assert "No security email address is published" in text


def test_security_reporter_section_publishes_one_usable_channel() -> None:
    """The reporter-facing region offers Private Vulnerability Reporting and nothing else.

    The URL guard above catches the specific link that regressed. It would not catch the same
    failure restated in prose -- "open a confidential issue on the canonical instance", with no
    link -- which is an unusable instruction that reads exactly like a usable one.
    """
    section = prose(security_reporter_section())

    assert PVR_URL in section
    assert "only published reporting channel" in section
    assert "confidential issue" not in section.lower(), (
        "the reporter-facing section sends a stranger to a channel they cannot reach"
    )

    # The maintainer path is correct advice, and it belongs outside the reporter's region rather
    # than deleted -- so assert it is still somewhere in the document.
    assert "confidential issue" in SECURITY.read_text(encoding="utf-8").lower()


def test_readme_carries_a_short_source_publication_note() -> None:
    """The publication model is visible in the README, as a note rather than a section.

    It sits with the documentation index, not ahead of Quick Start: repository governance is
    secondary to understanding and running the project, and leading with it costs every reader
    something to serve a minority of them.
    """
    text = README.read_text(encoding="utf-8")
    note = prose(readme_note())

    # A reader reaches the project before they reach its governance.
    assert text.index(README_NOTE_MARKER) > text.index("## Quick Start")
    assert "## Canonical Source & Public Mirror" not in text, (
        "the long mirror-policy section is back in the README; its detail belongs in MIRRORING.md"
    )

    # The image is the one destination the mirror policy cannot supply, because a reader arriving
    # from the registry is already there and a reader arriving from the source needs the link.
    assert "https://hub.docker.com/r/mcknly/robot-dev-team" in note
    assert "docs/CONTRIBUTING.md" in note
    assert "SECURITY.md" in note
    assert "docs/MIRRORING.md" in note

    # Which SBOM describes the image you pulled. A reader finds the in-tree reference document
    # first and will otherwise assume it is the one bound to their digest.
    assert "sbom/sbom.spdx.json" in note


def test_contribution_path_names_the_intake_branch_consistently() -> None:
    """`rc` has to be the answer everywhere, and `main` has to be closed off everywhere.

    A contributor who opens against `main` has done work that cannot be merged as-is, so the two
    documents they read before opening a pull request must not disagree.
    """
    for path in (CONTRIBUTING, PULL_REQUEST_TEMPLATE):
        text = path.read_text(encoding="utf-8")
        assert f"`{INTAKE_BRANCH}`" in text, f"{path.name} never names the intake branch"
        assert "cannot be merged" in text, f"{path.name} does not close off main"

    contributing = CONTRIBUTING.read_text(encoding="utf-8")
    # The external path promises no CI. Promising checks that do not run is worse than promising
    # none, because the contributor waits for a signal that never arrives.
    assert "No CI runs on your pull request." in contributing
    assert "Signed-off-by" in contributing


def test_contribution_guide_publishes_the_certificate_it_requires() -> None:
    """A sign-off requirement has to link the text it certifies, not paraphrase it.

    The point of the DCO is that a contributor can read the representations they are making. A
    gloss is not those representations, and nothing in this tree vendors the certificate.
    """
    contributing = prose(CONTRIBUTING.read_text(encoding="utf-8"))

    assert DCO_URL in contributing
    # No Action checks the trailer, because no CI runs on the pull request, so the document has to
    # say who does -- otherwise a contributor reasonably assumes a bot will tell them.
    assert "No automated check" in contributing


def test_contribution_guide_handles_the_default_pull_request_base() -> None:
    """GitHub bases a pull request on `main`, and no setting makes `rc` the default base.

    Telling a contributor to target `rc` without telling them the form will be pre-filled with
    `main` leaves the most likely mistake unmentioned in the one document meant to prevent it.
    """
    contributing = prose(CONTRIBUTING.read_text(encoding="utf-8"))

    assert f"{PUBLIC_REPO}/compare/{INTAKE_BRANCH}..." in contributing
    assert "default branch" in contributing


def test_mirror_policy_states_the_no_reverse_sync_rule() -> None:
    """The safeguard that has to be in place before the first public push.

    Everything else in the surface is a convenience. This is the rule that keeps the public
    repository from becoming a second source of truth or an independent release path.
    """
    text = prose(MIRRORING.read_text(encoding="utf-8"))

    assert "## 5. No reverse sync" in text
    assert "one-way" in text
    assert "GitHub Actions are not used to build" in text
    assert "append-only" in text


def test_mirror_policy_carries_the_history_and_sbom_explanations() -> None:
    """The three misreadings the policy exists to pre-empt, in the document that owns them.

    These moved out of the README with the long section. They still have to be somewhere a public
    reader can reach, because each of them is a claim about the published repository that reads
    wrong without its explanation.
    """
    text = prose(MIRRORING.read_text(encoding="utf-8"))

    # A fast-forward onto an untouched legacy prefix, which without explanation reads as a rewrite.
    assert "one commit per release" in text
    assert "not a history rewrite" in text
    assert "release log" in text

    # Nothing is stripped on the way out -- the verifiable claim the as-is publish filter buys.
    assert "byte-identical to the tagged canonical tree" in text

    # Which SBOM describes the pulled image, stated so the in-tree file cannot be mistaken for it.
    assert "sbom/sbom.spdx.json" in text
    assert "it can lag the" in text, "the reference SBOM is described as current, which it is not"


def test_mirror_policy_gives_a_consumer_a_verification_recipe() -> None:
    """Somewhere public says how to check a pull, not only that evidence exists.

    This is the gap that motivated the work: a consumer had nowhere to verify the source of what
    they pulled. The operator runbooks describe pull-by-digest, but they are not written for a
    public reader.

    The recipe is scoped to the receipt because the receipt is what a release actually publishes.
    An earlier version routed step 2 through the release manifest, which is one of the four
    deferred assets -- a recipe whose first instruction is to read an unpublished document.

    The ban is on the *instruction*, not on the words. A section-wide ban on "release manifest"
    also banned it from the paragraph that has to admit the manifest is deferred, so the honest
    sentence was un-writable in the one place whose declared job is to state the gap plainly.
    """
    raw = MIRRORING.read_text(encoding="utf-8")
    heading = "### Verifying what you pulled"

    assert heading in raw
    section = raw.split(heading, 1)[1].split("\n### ", 1)[0]
    recipe = prose(section)

    assert "RepoDigests" in recipe, "no way to obtain the digest actually running"
    assert "receipt" in recipe

    steps = numbered_steps(section)
    assert len(steps) >= 3, f"the recipe has {len(steps)} numbered steps, so it is not a recipe"
    for number, step in enumerate(steps, start=1):
        assert "manifest" not in step.lower(), (
            f"verification step {number} routes a public reader through an unpublished asset"
        )

    # Every deferred asset is named where the gap is stated, including the manifest step 2 was
    # moved off. A reader who knows a release manifest exists otherwise gets no signal here that
    # it is deferred rather than simply forgotten.
    gap_marker = "What you cannot check yet"
    assert gap_marker in recipe, "the recipe does not state what a public reader cannot check"
    gap = recipe.split(gap_marker, 1)[1]
    for asset in ("release manifest", "SBOM", "vulnerability report", "evaluation"):
        assert asset in gap, f"the stated gap omits the deferred {asset}"


def test_mirror_policy_does_not_promise_evidence_bytes_it_cannot_publish() -> None:
    """The hostname rule and "publish the same evidence bytes" cannot both hold.

    The release manifest, SBOM, Grype report, and scan evaluation each carry the canonical host by
    construction -- `CI_REGISTRY_IMAGE` is registry-host-prefixed and reaches `source_image` and
    `image_reference`, the SPDX `name` that `sbom_publish` requires, the scanned `userInput` that
    binds the report to its digest, and `image_repository` in the evaluation. Promising those bytes
    verbatim would republish the hostname once per release, which no amount of scrubbing the
    tracked tree would offset.
    """
    text = prose(MIRRORING.read_text(encoding="utf-8"))

    assert "are not published yet" in text
    assert "by construction" in text
    assert "SHA-256" in text, "the deferred assets are not pinned by anything"

    for claim in (
        "the same release evidence bytes",
        "all the same bytes the canonical release published",
        "republished as GitHub Release assets",
    ):
        assert claim not in text, (
            f"the mirror policy promises evidence bytes it cannot publish: {claim!r}"
        )

    # A transformed document is a different document. Calling it "the same bytes" is the specific
    # regression the deferral exists to avoid trading for.
    assert "field-aware" in text
    assert "blind string replacement" in text

    # And a canonical SHA-256 is a check on the canonical bytes only. Offering it as the way to
    # validate a future sanitized variant would restate the byte-level promise this section just
    # retired: the transformation the policy requires guarantees a different hash.
    unemphasized = text.replace("*", "")
    assert "only the canonical bytes" in unemphasized
    assert "its own published hash" in unemphasized


def test_mirror_policy_states_the_outbound_host_gate() -> None:
    """The publication job checks its whole outbound surface, not only the tree.

    The tree-wide guard in this file cannot see a commit identity, a tag object, a release body, or
    an asset's bytes. Public `main` is append-only, so a release commit authored at the canonical
    host is the one path with no undo -- checking the author alone leaves the committer and the
    annotated-tag tagger open.
    """
    raw = MIRRORING.read_text(encoding="utf-8")
    heading = "### The outbound host gate"

    assert heading in raw
    gate = prose(raw.split(heading, 1)[1].split("\n## ", 1)[0])

    assert "fails closed" in gate
    # Specific enough that deleting the requirement fails this. `"tree" in gate` matched incidental
    # prose and would have survived the projected tree dropping out of the surface entirely.
    for outbound in ("projected tree", "release body", "receipt", "every published asset"):
        assert outbound in gate, f"the gate does not cover the {outbound}"
    for identity in ("author", "committer", "tagger"):
        assert identity in gate

    # Two hosts, not one. GitLab permits the registry on its own host or subdomain, and the four
    # deferred assets carry `CI_REGISTRY_IMAGE` rather than the server host -- a gate spelled
    # against `CI_SERVER_HOST` alone passes every one of them on a split-host deployment. The
    # backticks matter: bare "CI_REGISTRY" is a substring of "CI_REGISTRY_IMAGE".
    assert "`CI_SERVER_HOST`" in gate
    assert "`CI_REGISTRY`" in gate, (
        "the outbound gate is specified against the server host alone, so a registry on a "
        "separate host is never checked"
    )


def test_mirror_policy_does_not_claim_the_hostname_is_secret() -> None:
    """The rule is target-list hygiene, and the document has to say so.

    The GitHub organisation, the Docker Hub repository, and the canonical namespace path are all
    public, so the host is a short guess. A policy that described the omission as secrecy would
    invite something to be built on an assumption that does not hold.
    """
    raw = MIRRORING.read_text(encoding="utf-8")
    heading = "## 1. Authority model"

    assert heading in raw
    section = prose(raw.split(heading, 1)[1].split("\n## ", 1)[0])

    # Emphasis is not the claim. `**not** secrecy` and `not secrecy` say the same thing, and
    # asserting on the asterisks turns an innocent rewrap into a build failure.
    assert "not secrecy" in section.replace("*", "")
    assert "grep" in section


def test_mirror_policy_separates_the_release_line_from_public_intake() -> None:
    """Public `main` is the read-only projection; `rc`, issues, and pull requests are not.

    Calling the whole repository read-only contradicts the same document's `rc` rules and the
    contribution guide it links to, and it is wrong in exactly the place a reader is invited to
    write. A test that merely required the word `rc` accepted that contradiction.
    """
    text = prose(MIRRORING.read_text(encoding="utf-8"))

    assert "release line" in text
    assert "public intake" in text
    assert "no transformed public release tree" in text

    # `rc` advances by fast-forward. A reset would drop accepted commits that are merged into `rc`
    # after a release commit was built but before it was published.
    assert "fast-forward only" in text
    assert "Reset forward" not in text


def test_mirror_policy_states_what_early_intake_actually_costs() -> None:
    """Merging into `rc` early fixes the ancestry, not the shipped code.

    The rule -- qualify on the canonical instance first -- stands. The reason originally given for
    it did not: a release commit's tree is the canonical tagged tree, so it omits an unqualified
    `rc` change on its own, and a forward revert undoes the intake merge before any projection. The
    discarded wording also offered force-pushing `rc` as the alternative, which contradicts the
    append-only, forward-only recovery this same document requires everywhere else.
    """
    text = MIRRORING.read_text(encoding="utf-8")
    section = prose(text.split("## 5. No reverse sync", 1)[1].split("\n## ", 1)[0])

    assert "irreversible is the **ancestry**, not the code" in section
    assert "never forced to ship" in section
    assert "forward revert" in section
    assert "force-push" not in section.lower(), (
        "the no-reverse-sync rule offers a history rewrite as a way out of an early merge"
    )


def test_mirror_policy_admits_no_publisher_before_the_automation() -> None:
    """Section 7's status has to agree with the authority model above it.

    Sections 1 and 3 name release automation as the only writer of public `main`, the stable tags,
    and the release artifacts, and no publication job exists yet. A status line that hands the
    interval to manual publication by a maintainer therefore names a publisher this policy forbids
    -- and that interval is exactly the one the project is in right now.
    """
    text = MIRRORING.read_text(encoding="utf-8")
    section = prose(text.split("## 7. Drift detection and recovery", 1)[1].split("\n## ", 1)[0])

    assert "no public publication happens at all" in section
    assert "not by hand" in section
    assert "manual publication" not in prose(text).lower(), (
        "the mirror policy authorizes a manual publication path its own authority model forbids"
    )


def test_mirror_policy_credentials_match_the_installed_scope() -> None:
    """Section 6 names the jobs that hold the token, and the pipeline has to agree with it.

    The section used to disclaim itself because nothing implemented it. Now that the publication
    jobs exist, the failure to guard against is the opposite drift: a policy that names two holders
    while the pipeline grants a third, or that still disclaims a control the pipeline installs.
    """
    text = MIRRORING.read_text(encoding="utf-8")
    section = prose(text.split("## 6. Credentials", 1)[1].split("\n## ", 1)[0])

    assert "not** yet a description" not in section, "section 6 still disclaims installed state"
    assert "`GITHUB_MIRROR_TOKEN`" in section
    assert "test_only_approved_jobs_receive_scoped_credentials" in section

    config = yaml.safe_load((REPO_ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    declaring = {
        name
        for name, entry in config.items()
        if isinstance(entry, dict)
        and isinstance(entry.get("environment"), dict)
        and entry["environment"].get("name") == "github-publication"
    }
    assert declaring == {"github_release_publish", "github_release_withdraw"}
    for name in declaring:
        assert f"`{name}`" in section, f"section 6 does not name {name} as a token holder"
    # The audit reads anonymously, and the policy says so; granting it the token would be drift.
    assert "environment" not in config["github_publication_audit"]
    assert "anonymously" in section


def test_mirror_policy_checklist_gates_the_cutover() -> None:
    """The owner checklist orders the two steps whose order is load-bearing.

    Private Vulnerability Reporting is a repository setting and must precede the first push,
    because the published `SECURITY.md` names a URL that 404s until it exists. Issues must follow
    the first projection, because GitHub reads the chooser from the default branch and Issues
    enabled earlier are blank issues with no chooser -- the exact first-contact path the chooser
    exists to close.
    """
    text = MIRRORING.read_text(encoding="utf-8")
    checklist = prose(text.split("## 8. Owner-side setup checklist", 1)[1].split("\n## ", 1)[0])

    assert "before the first public push" in checklist
    assert "only after" in checklist
    assert checklist.index("Private Vulnerability Reporting") < checklist.index("Issues enabled")

    # The ancestry promise is a repository setting, not a property of the projection: a squash
    # merge omits the contributor's commits and a rebase merge rewrites their identities.
    assert "squash merging and rebase merging disabled" in checklist

    # Section 4 explains the history shape in terms of this commit; the checklist creates `rc` at
    # it. Naming it in both is what keeps them describing the same point in history.
    assert LEGACY_PREFIX_COMMIT in checklist
    assert LEGACY_PREFIX_COMMIT in text.split("## 4.", 1)[1].split("\n## ", 1)[0]


def test_no_tracked_file_names_the_canonical_instance_by_hostname() -> None:
    """Zero contiguous occurrences of the private hostname in the published tree.

    This is the enforcement for the whole policy described at `PRIVATE_HOST`, and it replaces a
    parametrized check over the public-surface files alone. That earlier scope left the two places
    the host actually reached a stranger unguarded: `gitlab-connect` and `glab-usr` are copied into
    the published image, and the operator runbooks are mirrored to the same readers as everything
    else. Neither set was covered, so both could regress without failing a test.

    Bytes, not decoded text, because the published tree is not only markdown and a file that fails
    to decode should fail this check rather than skip it.

    Three things this deliberately does not do, now that it is the sole enforcement for the rule:
    it does not skip when git is unavailable, it does not silently drop a tracked path that is
    absent from the working tree, and it does not match case-sensitively. A skip reads as green, a
    dropped path is an unscanned file, and a title-cased spelling of the host resolves exactly
    like the lowercase one -- markdown prose capitalizes at a sentence start. (That last example
    is deliberately not written out here; this file is in scope for its own check.)
    """
    tracked = tracked_files()
    assert tracked is not None, (
        "git could not list the tracked files, so the hostname guard could not run. This fails "
        "rather than skips: it is the only check for the rule, and a skip reads as a pass."
    )

    needle = PRIVATE_HOST.encode()
    offenders: list[str] = []
    unreadable: list[str] = []
    scanned = 0

    for relative in sorted(tracked):
        path = REPO_ROOT / relative
        if not path.is_file():
            unreadable.append(relative)
            continue
        scanned += 1
        if needle in path.read_bytes().lower():
            offenders.append(relative)

    assert not offenders, (
        "the canonical instance is named by hostname in the published tree: "
        + ", ".join(offenders)
    )
    assert not unreadable, (
        "tracked paths could not be read, so they were never checked for the hostname: "
        + ", ".join(unreadable)
    )
    assert scanned == len(tracked), (
        f"{scanned} of {len(tracked)} tracked files were scanned"
    )


def test_readme_note_agrees_with_the_mirror_policy_on_release_evidence() -> None:
    """The most visible public surface must not promise what the policy defers.

    The README is the public entry point and is copied into the runtime image, so a stranger reads
    its assurance before they ever reach `docs/MIRRORING.md`. It previously said every GitHub
    Release carries the SBOM and vulnerability evidence for the published digest -- the exact
    guarantee the policy establishes cannot be kept while the hostname rule holds. Two mutually
    exclusive release contracts in one published tree is worse than either one alone.
    """
    note = prose(readme_note()).replace("*", "")
    policy = prose(MIRRORING.read_text(encoding="utf-8"))

    assert "receipt" in note, "the README does not name what a release actually publishes"
    assert "not published yet" in note, "the README does not state the evidence gap"
    assert "SHA-256" in note, "the README states the gap without saying what still pins the bytes"

    for claim in (
        "carries the SBOM and vulnerability evidence",
        "SBOM and vulnerability evidence for the exact image digest",
    ):
        assert claim not in note, (
            f"the README promises release evidence the mirror policy defers: {claim!r}"
        )

    # The policy is the document that owns the deferral; if it stops deferring, this test is
    # asserting the README into agreement with something that no longer exists.
    assert "are not published yet" in policy


# The first release published to GitHub. Its changelog section, and every section written after
# it, becomes a public GitHub Release body; the sections before it never did.
FIRST_PUBLIC_RELEASE = "v0.3.0"


def public_changelog_sections() -> dict[str, str]:
    """Every changelog section that is, or will become, a public release body, by heading."""
    text = CHANGELOG.read_text(encoding="utf-8")
    sections: dict[str, str] = {}
    for chunk in text.split("\n## ")[1:]:
        heading, _, body = chunk.partition("\n")
        sections[heading.strip()] = prose(body)
        if heading.startswith(f"[{FIRST_PUBLIC_RELEASE}]"):
            return sections
    raise AssertionError(f"docs/CHANGELOG.md has no [{FIRST_PUBLIC_RELEASE}] section")


def test_public_changelog_sections_agree_with_the_mirror_policy() -> None:
    """A section that becomes a public release body is public surface.

    `docs/MIRRORING.md` publishes "a GitHub Release whose body is the changelog section for that
    version". An entry that tells a public reader to match their digest against the release
    manifest and read the digest-keyed SBOM on the same release is therefore an instruction to
    fetch three documents that release does not publish -- delivered on the release page itself.
    The rule follows the notes out of `[Unreleased]` when a release dates them, and covers every
    section from the first public release on.

    A test a public section cites must keep existing, and that is deliberate. A published release
    body names those tests as the enforcement behind its guarantees, and it cannot be edited
    afterwards. Renaming one would leave a public claim pointing at nothing, so keep the old name.
    """
    sections = public_changelog_sections()
    assert "[Unreleased]" in sections

    for heading, body in sections.items():
        for claim in (
            "match it against the release manifest",
            "read the digest-keyed SBOM and scan evidence on the same release",
            "carries the SBOM and vulnerability evidence",
        ):
            assert claim not in body, (
                f"changelog section {heading} promises deferred release evidence: {claim!r}"
            )

    first = next(body for heading, body in sections.items() if heading.startswith(f"[{FIRST_PUBLIC_RELEASE}]"))
    assert "match it against the publication receipt" in first, (
        "the first public release's notes do not describe the recipe the policy actually states"
    )

    # Every test named in a public section has to exist. `test_public_surface_names_the_
    # canonical_instance_by_role` was cited here after it had been replaced, which reads as a
    # live guarantee and points a reader at nothing.
    suite = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(TESTS_DIR.glob("test_*.py"))
    )
    # Backtick-delimited and whole, so `tests/test_wrappers.py` is read as the module path it is
    # rather than as a citation of a test named `test_wrappers`.
    cited_anywhere = False
    for heading, body in sections.items():
        cited = sorted(set(re.findall(r"`(test_[a-z0-9_]+)`", body)))
        cited_anywhere = cited_anywhere or bool(cited)
        for name in cited:
            assert f"def {name}(" in suite, (
                f"changelog section {heading} cites {name}, which no test defines"
            )
    assert cited_anywhere, "no public changelog section cites a test, so this check proves nothing"


@pytest.mark.parametrize(
    "path",
    [README, *PUBLIC_SURFACE_FILES, *issue_form_paths()],
    ids=lambda p: p.name,
)
def test_public_surface_scopes_read_only_to_the_release_line(path: Path) -> None:
    text = prose(path.read_text(encoding="utf-8")).lower()

    for claim in UNSCOPED_READ_ONLY_CLAIMS:
        assert claim.lower() not in text, (
            f"{path.name} calls the whole public repository read-only, which contradicts the "
            f"`{INTAKE_BRANCH}` intake branch the same surface sends contributors to"
        )
