FROM node:22-bookworm-slim AS opencode-build

ARG OPENCODE_API_VERSION=v1
ARG OPENCODE_VERSION=1.18.25
ARG OPENCODE_V2_VERSION=2.3.0
RUN case "${OPENCODE_API_VERSION}" in \
        v1) npm install --global --omit=dev "opencode-ai@${OPENCODE_VERSION}" ;; \
        v2) npm install --global --omit=dev "@opencode/cli@${OPENCODE_V2_VERSION}" ;; \
        *) exit 1 ;; \
    esac \
    && cp -L /usr/local/bin/opencode /tmp/opencode-launcher

FROM ubuntu:24.04

ARG OPENCODE_API_VERSION=v1
ARG DEBIAN_FRONTEND=noninteractive
ARG APP_UID=1000
ARG APP_GID=1000
ARG CONAN_VERSION=2.27.0
ARG GIT_USER_NAME="OpenCode Web"
ARG GIT_USER_EMAIL="opencode-web@localhost"

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        cmake \
        curl \
        git \
        gnupg \
        ninja-build \
        nodejs \
        npm \
        openssh-client \
        pkg-config \
        python3.12 \
        python3.12-venv \
        ripgrep \
    && rm -rf /var/lib/apt/lists/*

COPY --from=opencode-build /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=opencode-build /tmp/opencode-launcher /usr/local/bin/opencode
COPY --from=opencode-build /usr/local/bin/node /usr/local/bin/node
RUN ln -s /usr/local/bin/opencode /usr/bin/opencode \
    && /usr/bin/opencode --version \
    && if id ubuntu >/dev/null 2>&1; then userdel --remove ubuntu; fi \
    && groupadd --gid "${APP_GID}" appuser \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --shell /bin/bash appuser \
    && mkdir -p /home/appuser/.pyenv/versions/3.12.3/bin \
    && ln -s /usr/bin/python3.12 /home/appuser/.pyenv/versions/3.12.3/bin/python3.12

ENV HOME=/home/appuser \
    OPENCODE_API_VERSION=${OPENCODE_API_VERSION} \
    PATH=/opt/venv/bin:/home/appuser/.local/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN python3.12 -m venv /opt/venv
COPY pyproject.toml README.md ./
COPY src ./src
COPY opencode/config/package.json opencode/config/package-lock.json /home/appuser/.config/opencode/
COPY mcp/mcp-memory-server/requirements.txt /tmp/mcp-memory-requirements.txt
COPY mcp/mcp-gitlab-readonly/requirements.txt /tmp/mcp-gitlab-requirements.txt
COPY mcp/mcp-consultation-artifacts/requirements.txt /tmp/mcp-artifacts-requirements.txt
RUN /opt/venv/bin/pip install --no-cache-dir . \
    && /opt/venv/bin/pip install --no-cache-dir "conan==${CONAN_VERSION}" \
    && npm --prefix /home/appuser/.config/opencode ci --omit=dev \
    && python3.12 -m venv /home/appuser/mcp/mcp-memory-server/venv \
    && /home/appuser/mcp/mcp-memory-server/venv/bin/pip install \
        --no-cache-dir -r /tmp/mcp-memory-requirements.txt \
    && python3.12 -m venv /home/appuser/mcp/mcp-gitlab-readonly/venv \
    && /home/appuser/mcp/mcp-gitlab-readonly/venv/bin/pip install \
        --no-cache-dir -r /tmp/mcp-gitlab-requirements.txt \
    && python3.12 -m venv /home/appuser/mcp/mcp-consultation-artifacts/venv \
    && /home/appuser/mcp/mcp-consultation-artifacts/venv/bin/pip install \
        --no-cache-dir -r /tmp/mcp-artifacts-requirements.txt \
    && mkdir -p \
        /app/data \
        /app/.secrets \
        /home/appuser/.cache/opencode \
        /home/appuser/.conan2 \
        /home/appuser/.local/share/opencode \
        /home/appuser/.local/state/opencode \
    && git config --file /home/appuser/.gitconfig user.name "${GIT_USER_NAME}" \
    && git config --file /home/appuser/.gitconfig user.email "${GIT_USER_EMAIL}" \
    && git config --file /home/appuser/.gitconfig commit.gpgSign false \
    && chown -R appuser:appuser /app/data /home/appuser
COPY --chown=appuser:appuser mcp /home/appuser/mcp
COPY --chown=appuser:appuser opencode/config /home/appuser/.config/opencode
COPY docker/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY docker/git-gpg-sign /usr/local/bin/git-gpg-sign
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh /usr/local/bin/git-gpg-sign

USER appuser
EXPOSE 8081

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["opencode-web"]
