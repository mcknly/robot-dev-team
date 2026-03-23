# Robot Dev Team Project
# File: Dockerfile
# Description: Container image for the Robot Dev Team Project webhook service.
# License: MIT
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 MCKNLY LLC

ARG PYTHON_IMAGE=python:3.12.11-slim-bookworm
FROM ${PYTHON_IMAGE}

ARG PIP_VERSION=26.0.1
ARG UV_VERSION=0.10.10
ARG GLAB_VERSION=1.89.0
ARG NODE_VERSION=20.20.1
ARG DEBIAN_SNAPSHOT=20260315T000000Z
ARG SYFT_VERSION=1.42.2

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# OS dependencies pinned via Debian snapshot
RUN set -eux; \
    echo "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}/ bookworm main" > /etc/apt/sources.list; \
    echo "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}/ bookworm-updates main" >> /etc/apt/sources.list; \
    echo "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}/ bookworm-security main" >> /etc/apt/sources.list; \
    printf 'Acquire::Check-Valid-Until "false";\nAcquire::Retries "5";\n' > /etc/apt/apt.conf.d/99snapshot; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      bash \
      ca-certificates \
      curl \
      git \
      gosu \
      procps \
      tini \
      xz-utils; \
    rm -rf /var/lib/apt/lists/*

# Install Node.js runtime
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "${arch}" in \
      amd64) node_selector="linux-x64" ;; \
      arm64) node_selector="linux-arm64" ;; \
      armhf) node_selector="linux-armv7l" ;; \
      *) echo "unsupported architecture for Node.js: ${arch}" >&2; exit 1 ;; \
    esac; \
    node_dir="node-v${NODE_VERSION}-${node_selector}"; \
    node_url="https://nodejs.org/dist/v${NODE_VERSION}/${node_dir}.tar.xz"; \
    curl -fsSL "${node_url}" -o "/tmp/${node_dir}.tar.xz"; \
    curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt" -o /tmp/SHASUMS256.txt; \
    (cd /tmp && grep " ${node_dir}.tar.xz$" SHASUMS256.txt > node.sha256); \
    (cd /tmp && sha256sum -c node.sha256); \
    tar -xJf "/tmp/${node_dir}.tar.xz" -C /usr/local --strip-components=1; \
    rm "/tmp/${node_dir}.tar.xz" /tmp/SHASUMS256.txt /tmp/node.sha256; \
    node --version; \
    npm --version

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
    uv pip install --system /work

COPY prompts/ /work/prompts/
COPY config/ /work/config/
COPY scripts/ /work/scripts/
RUN mkdir -p /work/run-logs

# Generate SBOM for reproducibility records
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "${arch}" in \
      amd64) syft_arch="linux_amd64" ;; \
      arm64) syft_arch="linux_arm64" ;; \
      *) echo "unsupported architecture for syft: ${arch}" >&2; exit 1 ;; \
    esac; \
    syft_tar="syft_${SYFT_VERSION}_${syft_arch}.tar.gz"; \
    syft_url="https://github.com/anchore/syft/releases/download/v${SYFT_VERSION}/${syft_tar}"; \
    curl -fsSL "${syft_url}" -o "/tmp/${syft_tar}"; \
    curl -fsSL "https://github.com/anchore/syft/releases/download/v${SYFT_VERSION}/syft_${SYFT_VERSION}_checksums.txt" -o /tmp/syft_checksums.txt; \
    (cd /tmp && grep " ${syft_tar}$" syft_checksums.txt > syft.sha256); \
    (cd /tmp && sha256sum -c syft.sha256); \
    tar -xzf "/tmp/${syft_tar}" -C /usr/local/bin syft; \
    chmod +x /usr/local/bin/syft; \
    mkdir -p /work/sbom; \
    syft scan dir:/ --scope all-layers --output spdx-json=/work/sbom/sbom.spdx.json; \
    rm /usr/local/bin/syft; \
    rm "/tmp/${syft_tar}" /tmp/syft_checksums.txt /tmp/syft.sha256

ENV HOME=/home/appuser \
    NPM_CONFIG_PREFIX=/home/appuser/.npm-global \
    PATH="/home/appuser/.local/bin:/home/appuser/.npm-global/bin:${PATH}"

RUN useradd -u 10001 -ms /bin/bash appuser && chown -R appuser:appuser /work /home/appuser

EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--", "docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
