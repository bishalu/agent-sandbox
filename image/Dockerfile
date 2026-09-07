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
        sudo tini \
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

# Claude Code, via the supported npm distribution.
RUN npm install -g @anthropic-ai/claude-code && npm cache clean --force

# Unprivileged user available on request. uid 1000 matches the common host uid.
RUN useradd -m -u 1000 -s /bin/bash agent || true

# Git needs to trust a bind-mounted worktree owned by a different uid mapping.
RUN git config --system --add safe.directory /workspace \
    && git config --system --add safe.directory '*'

WORKDIR /workspace

# tini reaps zombies, so a killed agent cannot leave orphans behind.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["bash"]
