"""Robot Dev Team Project
File: tests/test_runtime_install_contract.py
Description: The published runtime-install contract agrees with the installers and the image.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import preflight
from app.services.routes import RouteRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
DOCKERFILE = REPO_ROOT / "Dockerfile"
DEPENDENCY_MANAGEMENT = REPO_ROOT / "docs" / "DEPENDENCY_MANAGEMENT.md"
RELEASING = REPO_ROOT / "docs" / "RELEASING.md"
SMOKE_SCRIPT = SCRIPTS / "ci-smoke-image.sh"
GENERATE_SBOM = SCRIPTS / "generate-sbom.sh"
STOCK_ROUTES = REPO_ROOT / "config" / "routes.yaml"

EGRESS_HEADING = "### Boot-time egress"
PUBLIC_REPO = "https://github.com/mcknly/robot-dev-team"
SNAPSHOT_LABEL = "com.mcknly.robot-dev-team.debian-snapshot"
SOURCE_LABEL = "com.mcknly.robot-dev-team.debian-source"
# The two archive roots the Dockerfile's APT sources use, spelled against the ARG.
SNAPSHOT_ROOTS = (
    "http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}/",
    "http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}/",
)

# A bare DNS name in backticks, optionally with a leading `*.` wildcard. The TLD must be letters,
# which keeps file names such as `SHASUMS256.txt` or `manifest.json` from reading as hosts only
# together with the lowercase-only rule; paths never match because `/` is not allowed.
HOST_TOKEN = re.compile(r"(?:\*\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}")
URL_HOST = re.compile(r"https?://([A-Za-z0-9.-]+)")
BACKTICKED = re.compile(r"`([^`]+)`")
SYFT_PIN = re.compile(r"anchore/syft:v[0-9.]+@sha256:[0-9a-f]{64}")
INVENTORY_HEADING = "### Inventory a running container"
# Installers that pipe a vendor script which then runs an `install` step nobody has traced.
VENDOR_INSTALL_STEP = {"install-claude.sh": "`claude install`", "install-gemini.sh": "`agy install"}


def egress_cells() -> dict[str, list[str]]:
    """The boot-time egress table's cells, keyed by installer file name."""
    text = DEPENDENCY_MANAGEMENT.read_text(encoding="utf-8")
    assert EGRESS_HEADING in text, "docs/DEPENDENCY_MANAGEMENT.md has no boot-time egress section"
    section = text.split(EGRESS_HEADING, 1)[1].split("\n## ", 1)[0]
    rows: dict[str, list[str]] = {}
    for line in section.splitlines():
        if not line.startswith("|") or set(line) <= set("|-: "):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        assert len(cells) == 4, f"egress table row does not have four cells: {line}"
        installer = re.search(r"scripts/(install-[a-z0-9-]+\.sh)", cells[1])
        if installer is None:
            continue  # the header row
        rows[installer.group(1)] = cells
    assert rows, "the boot-time egress table has no rows"
    return rows


def egress_rows() -> dict[str, tuple[str, set[str]]]:
    """The boot-time egress table, keyed by installer file name: (stock-route cell, hosts)."""
    return {
        installer: (
            cells[2],
            {token for token in BACKTICKED.findall(cells[3]) if HOST_TOKEN.fullmatch(token)},
        )
        for installer, cells in egress_cells().items()
    }


def installer_url_hosts(path: Path) -> set[str]:
    """Hosts named in an installer's executable lines. Comments are prose, not fetches."""
    hosts: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        hosts.update(host.lower() for host in URL_HOST.findall(line))
    return hosts


def stock_installers() -> set[str]:
    """Installers the stock route file selects, resolved the way preflight resolves them."""
    registry = RouteRegistry(str(STOCK_ROUTES))
    binaries = {
        preflight._route_binary(task.options, task.agent)
        for _, task in registry.iter_agent_tasks()
    }
    return {
        installer.path.name
        for installer in preflight.discover_installers(SCRIPTS)
        if binaries.intersection(installer.provides)
    }


def test_every_installer_has_an_egress_row() -> None:
    """A consumer builds their allowlist from one table, so no installer may be missing from it."""
    installers = {path.name for path in SCRIPTS.glob("install-*.sh")}
    rows = egress_rows()
    assert installers == set(rows), (
        f"installers without an egress row: {sorted(installers - set(rows))}; "
        f"rows for installers that do not exist: {sorted(set(rows) - installers)}"
    )


@pytest.mark.parametrize(
    "installer", sorted(path.name for path in SCRIPTS.glob("install-*.sh"))
)
def test_every_host_an_installer_fetches_is_in_its_row(installer: str) -> None:
    """A URL added to an installer without its host in the table breaks every strict allowlist.

    This covers what this repository controls. Hosts chosen by vendor scripts, manifests, and
    redirects cannot be checked offline; the table marks those as observed, with a date.
    """
    _, hosts = egress_rows()[installer]
    missing = installer_url_hosts(SCRIPTS / installer) - hosts
    assert not missing, (
        f"scripts/{installer} fetches from {sorted(missing)}, which its row in the boot-time "
        f"egress table (docs/DEPENDENCY_MANAGEMENT.md) does not list"
    )


def test_stock_route_column_matches_the_shipped_routes() -> None:
    """"Enabled" in the table has to mean what the shipped route file actually installs."""
    expected = stock_installers()
    assert expected, "the stock route file selects no installer"
    for installer, (stock, _) in egress_rows().items():
        enabled = "enabled" in stock and "commented out" not in stock
        assert enabled == (installer in expected), (
            f"the egress table says {installer} is {stock!r} under the stock routes, but "
            f"config/routes.yaml {'does' if installer in expected else 'does not'} install it"
        )


@pytest.mark.parametrize("installer", sorted(VENDOR_INSTALL_STEP))
def test_vendor_install_steps_stay_marked_untraced(installer: str) -> None:
    """These rows list the hosts the vendor script fetches, not what its `install` step contacts.

    Dropping the caveat would turn a partial list into an allowlist that looks complete, which
    fails a fresh boot the first time the vendor binary reaches somewhere new.
    """
    host_cell = egress_cells()[installer][3]
    assert VENDOR_INSTALL_STEP[installer] in host_cell
    assert "not been traced" in host_cell, (
        f"the egress row for {installer} no longer says its vendor install step is untraced"
    )


def test_every_installer_curl_bounds_its_connect_time() -> None:
    """A dropping firewall otherwise stalls each fetch for the kernel's SYN retry window."""
    unbounded: list[str] = []
    for path in sorted(SCRIPTS.glob("install-*.sh")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#") or not re.search(r"\bcurl -", line):
                continue
            if "--connect-timeout" not in line:
                unbounded.append(f"{path.name}:{number}")
    assert not unbounded, f"curl calls without --connect-timeout: {unbounded}"


def dockerfile_labels() -> dict[str, str]:
    text = DOCKERFILE.read_text(encoding="utf-8")
    match = re.search(r"^LABEL (.+?)(?<!\\)\n", text, re.MULTILINE | re.DOTALL)
    assert match, "the Dockerfile sets no LABEL"
    return dict(re.findall(r'([a-z0-9.-]+)="([^"]*)"', match.group(1)))


def test_image_labels_carry_the_source_pointer_from_the_build_args() -> None:
    """The corresponding-source pointer is read from the pulled image, so it must be the build's.

    A literal timestamp would drift from `DEBIAN_SNAPSHOT` on the next snapshot bump and point a
    consumer at the wrong archive for the packages they actually received.
    """
    labels = dockerfile_labels()
    assert labels.get("org.opencontainers.image.source") == PUBLIC_REPO
    assert labels.get(SNAPSHOT_LABEL) == "${DEBIAN_SNAPSHOT}"
    assert labels.get(SOURCE_LABEL, "").split() == list(SNAPSHOT_ROOTS)

    text = DOCKERFILE.read_text(encoding="utf-8")
    for root in SNAPSHOT_ROOTS:
        assert root in text.split("LABEL ", 1)[0], (
            f"the label names {root}, which the APT sources above it no longer use"
        )
    # The ARG has to be in scope for the label: declared after FROM, before the LABEL.
    arg = text.index("ARG DEBIAN_SNAPSHOT=")
    assert text.index("\nFROM ") < arg < text.index("\nLABEL ")


def test_smoke_test_checks_the_label_the_dockerfile_sets() -> None:
    """The unit test sees the Dockerfile's text; the smoke test sees the built config."""
    smoke = SMOKE_SCRIPT.read_text(encoding="utf-8")
    assert f'.Config.Labels "{SNAPSHOT_LABEL}"' in smoke
    assert "ARG DEBIAN_SNAPSHOT=" in smoke


def test_running_container_inventory_uses_the_pinned_syft() -> None:
    """The documented running-container scan must be the same Syft the release SBOM comes from."""
    pinned = SYFT_PIN.findall(GENERATE_SBOM.read_text(encoding="utf-8"))
    documented = SYFT_PIN.findall(RELEASING.read_text(encoding="utf-8"))
    assert len(set(pinned)) == 1, "scripts/generate-sbom.sh does not pin exactly one Syft image"
    assert documented, "docs/RELEASING.md gives no pinned Syft recipe for a running container"
    assert set(documented) == set(pinned), (
        f"docs/RELEASING.md documents {sorted(set(documented))}, but scripts/generate-sbom.sh "
        f"pins {pinned[0]}"
    )


def test_running_container_inventory_never_captures_the_container_config() -> None:
    """`docker commit` records the Compose-injected environment -- every agent token and the
    webhook secret -- and `docker image save` writes it to disk. The recipe must export the
    filesystem instead, and must delete the export however the scan ends.
    """
    text = RELEASING.read_text(encoding="utf-8")
    assert INVENTORY_HEADING in text, "docs/RELEASING.md has no running-container inventory"
    section = text.split(INVENTORY_HEADING, 1)[1].split("\n## ", 1)[0]
    commands = "\n".join(re.findall(r"```bash\n(.*?)```", section, re.DOTALL))
    assert "docker export" in commands
    assert "docker commit" not in commands
    assert "image save" not in commands
    assert re.search(r"trap '[^']*rm -rf \"\$scratch\"' EXIT", commands), (
        "the inventory recipe does not delete its export on exit"
    )
    assert SYFT_PIN.search(commands), "the inventory recipe does not run the pinned Syft"
