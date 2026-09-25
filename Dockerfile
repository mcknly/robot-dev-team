# Robot Dev Team Project
# File: Dockerfile
# Description: Container image for the Robot Dev Team Project webhook service.
# License: MIT
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 MCKNLY LLC

ARG PYTHON_IMAGE=python:3.14.7-slim-trixie@sha256:656d12e70054d5fda18a045e2494c96701e9792dd1445f95b3d038df954f57e9
FROM ${PYTHON_IMAGE}

ARG PIP_VERSION=26.2
ARG UV_VERSION=0.12.1
ARG GLAB_VERSION=1.111.0
ARG DEBIAN_SNAPSHOT=20260915T194013Z

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# OS dependencies pinned via Debian snapshot. The base image ships its own deb822 source
# pointing at the live mirrors, so it is cleared first: APT reads sources.list.d/ as well as
# sources.list, and loading both leaves the snapshot governing nothing. The check after
# `update` enforces the property -- every active index is under the snapshot -- rather than
# the filename, so a future base-image layout change cannot quietly reintroduce a live mirror.
# The upgrade step covers the debs the base image ships preinstalled, which `install` alone
# never touches; the snapshot is frozen, so it resolves to the same versions on every rebuild.
# `util-linux` supplies setpriv, which docker-entrypoint.sh uses for the privilege drop. It is a
# Required-priority package and is already present, so apt reports 0 newly installed -- naming it
# is what binds it to the snapshot and stops a base-image change from silently dropping setpriv.
# It replaced `gosu`, whose bookworm build is statically linked against an EOL Go 1.19.8
# toolchain that no snapshot bump could move, because Debian would not rebuild it (see #58).
# Trixie does ship a gosu rebuilt on a current toolchain, but setpriv stays: it carries no Go
# runtime and therefore no Go-stdlib CVE surface at all, which is the stronger property and
# the one that does not decay as the new toolchain ages. `scripts/ci-smoke-image.sh` asserts
# gosu is absent, so reintroducing it on trixie is a regression rather than an alternative.
RUN set -eux; \
    rm -rf /etc/apt/sources.list.d/*; \
    echo "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}/ trixie main" > /etc/apt/sources.list; \
    echo "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}/ trixie-updates main" >> /etc/apt/sources.list; \
    echo "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}/ trixie-security main" >> /etc/apt/sources.list; \
    printf 'Acquire::Check-Valid-Until "false";\nAcquire::Retries "5";\n' > /etc/apt/apt.conf.d/99snapshot; \
    apt-get update; \
    apt-get indextargets --no-release-info 'Created-By: Packages' \
      | awk '/^URI:/ { print $2 }' | sort -u > /tmp/apt-sources.txt; \
    test -s /tmp/apt-sources.txt; \
    if grep -v '^http://snapshot\.debian\.org/' /tmp/apt-sources.txt; then \
      echo "ERROR: the APT sources listed above are outside the pinned snapshot" >&2; \
      exit 1; \
    fi; \
    DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
      -o Dpkg::Options::=--force-confold; \
    upgrade_plan="$(LC_ALL=C apt-get -s upgrade)"; \
    held="$(printf '%s\n' "${upgrade_plan}" | sed -n 's/^.* \([0-9][0-9]*\) not upgraded\.$/\1/p')"; \
    case "${held}" in \
      '' | *[!0-9]*) \
        echo "ERROR: could not read a held-back count from the apt-get upgrade" >&2; \
        echo "simulation below; refusing to assume nothing was held back" >&2; \
        printf '%s\n' "${upgrade_plan}" >&2; \
        exit 1; \
        ;; \
    esac; \
    if [ "${held}" -ne 0 ]; then \
      echo "ERROR: apt-get upgrade held back ${held} package(s); it never installs new" >&2; \
      echo "packages, so a fix that needs a new dependency is skipped with exit 0" >&2; \
      printf '%s\n' "${upgrade_plan}" >&2; \
      exit 1; \
    fi; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      bash \
      bzip2 \
      ca-certificates \
      curl \
      git \
      tini \
      util-linux \
      xz-utils; \
    rm -f /tmp/apt-sources.txt; \
    rm -rf /var/lib/apt/lists/*

# Install GitLab CLI (glab)
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "${arch}" in \
      amd64) glab_selector="linux_amd64" ;; \
      arm64) glab_selector="linux_arm64" ;; \
      armhf) glab_selector="linux_armv6" ;; \
      i386) glab_selector="linux_386" ;; \
      *) echo "unsupported architecture: ${arch}" >&2; exit 1 ;; \
    esac; \
    glab_deb="glab_${GLAB_VERSION}_${glab_selector}.deb"; \
    glab_url="https://gitlab.com/gitlab-org/cli/-/releases/v${GLAB_VERSION}/downloads/${glab_deb}"; \
    curl -sSL "${glab_url}" -o "/tmp/${glab_deb}"; \
    curl -sSL "https://gitlab.com/gitlab-org/cli/-/releases/v${GLAB_VERSION}/downloads/checksums.txt" -o /tmp/glab_checksums.txt; \
    (cd /tmp && grep " ${glab_deb}$" glab_checksums.txt > glab.sha256); \
    (cd /tmp && sha256sum -c glab.sha256); \
    dpkg -i "/tmp/${glab_deb}"; \
    rm "/tmp/${glab_deb}" /tmp/glab_checksums.txt /tmp/glab.sha256

WORKDIR /work

COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

COPY gitlab-connect /usr/local/bin/
COPY glab-usr /usr/local/bin/
RUN chmod +x \
    /usr/local/bin/gitlab-connect \
    /usr/local/bin/glab-usr

COPY pyproject.toml README.md uv.lock /work/
COPY app/ /work/app/

RUN pip install --no-cache-dir --upgrade "pip==${PIP_VERSION}" && \
    pip install --no-cache-dir "uv==${UV_VERSION}" && \
    uv sync --frozen --no-dev --no-install-project

COPY prompts/ /work/prompts/
COPY config/ /work/config/
COPY scripts/ /work/scripts/
RUN mkdir -p /work/run-logs

ENV HOME=/home/appuser \
    PATH="/opt/venv/bin:/home/appuser/.local/bin:${PATH}"

RUN useradd -u 10001 -ms /bin/bash appuser && chown -R appuser:appuser /work /home/appuser

EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--", "docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
