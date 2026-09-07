# agent-sandbox base image.
#
# One reusable image for general software development. Layers are ordered
# least-changing first so rebuilds reuse cache. Deliberately does NOT try to
# preinstall every ecosystem — agents install project dependencies inside
# their own disposable container.
#
# No credentials are ever baked in. Auth arrives at run time as a narrow
# read-only mount or an environment variable (see credentials.py).
#
# A note on users: the default run executes as UID 0 *inside the container*.
# Under rootless Docker that maps to the host user, so it is not host root,
# and it keeps files written into the bind-mounted workspace owned correctly.
# An unprivileged `agent` user is created for callers who want it.

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NODE_MAJOR=22

# System packages. One layer, cleaned in the same step so nothing lingers.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl wget gnupg git bash jq \
        build-essential pkg-config \
        python3 python3-pip python3-venv python3-dev \
        ripgrep fd-find \
        unzip zip tar gzip bzip2 xz-utils \
        less nano vim-tiny procps psmisc file openssh-client \
        sudo tini just sqlite3 \
    && ln -sf /usr/bin/fdfind /usr/local/bin/fd \
    && rm -rf /var/lib/apt/lists/*

# Node.js LTS from NodeSource + pnpm via corepack.
RUN curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /usr/share/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && corepack enable && corepack prepare pnpm@latest --activate

# GitHub CLI.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# uv and uvx, copied from the official image pinned by digest.
COPY --from=ghcr.io/astral-sh/uv:0.12.10@sha256:2bb3ebca0a796a155094a27773d290c4b074572e6107f171d88d086682fd2500 \
    /uv /uvx /usr/local/bin/

# Node toolchain: Claude Code and Pi, installed from the committed lockfile so
# the pin is the lockfile rather than a version string. Auto-update is off;
# IS_SANDBOX lets Claude Code run with bypass as container root, and
# CLAUDE_CONFIG_DIR keeps its state inside the mounted agent home.
COPY toolchain/package.json toolchain/package-lock.json /opt/agent-sandbox/toolchain/
RUN cd /opt/agent-sandbox/toolchain && npm ci --omit=dev && npm cache clean --force
ENV PATH=/opt/agent-sandbox/toolchain/node_modules/.bin:$PATH \
    DISABLE_AUTOUPDATER=1 \
    IS_SANDBOX=1 \
    CLAUDE_CONFIG_DIR=/root/.claude

# Pi agent template: the bridge package tree plus settings, seeded into each
# sandbox's agent home at run time. Built outside /root because /root is
# shadowed by the runtime home mount.
COPY pi-agent-template/ /opt/agent-sandbox/pi-agent-template/
RUN cd /opt/agent-sandbox/pi-agent-template/npm && npm ci --omit=dev && npm cache clean --force

# Fail the build if the pinned tools or the bridge template are not usable.
# The template is checked through a scratch copy so pi's first-run files
# (auth.json, models-store.json) do not end up in the template itself.
RUN test "$(claude --version | grep -o '[0-9][0-9.]*' | head -1)" = "2.1.263" \
    && test "$(pi --version | grep -o '[0-9][0-9.]*' | head -1)" = "0.85.1" \
    && cp -r /opt/agent-sandbox/pi-agent-template /tmp/pi-agent-check \
    && PI_CODING_AGENT_DIR=/tmp/pi-agent-check pi --list-models | grep -q claude-bridge \
    && rm -rf /tmp/pi-agent-check

# Unprivileged user available on request. uid 1000 matches the common host uid.
RUN useradd -m -u 1000 -s /bin/bash agent || true

# Git needs to trust a bind-mounted worktree owned by a different uid mapping.
RUN git config --system --add safe.directory /workspace \
    && git config --system --add safe.directory '*'

WORKDIR /workspace

# tini reaps zombies, so a killed agent cannot leave orphans behind.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["bash"]
