# Multi-stage build for MCP Smart Contract Auditor container
# This Dockerfile includes Slither (Python-based tool)
# Aderyn (Rust-based) is installed via Cyfrinup in this image

FROM python:3.12-slim

LABEL maintainer="farofino-mcp"
LABEL description="MCP Smart Contract Auditor with Slither and Aderyn pre-installed via Cyfrinup."

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ENV DEBIAN_FRONTEND=noninteractive
ENV SHELL=/bin/bash

# Install minimal system dependencies required by audit tooling and networking
RUN --mount=type=cache,target=/var/lib/apt/lists \
    --mount=type=cache,target=/var/cache/apt \
    apt-get update -o Acquire::Check-Valid-Until=false || \
    (apt-get clean && \
     rm -rf /var/lib/apt/lists/* && \
     apt-get update -o Acquire::Check-Valid-Until=false) && \
    apt-get install -y --no-install-recommends \
    ca-certificates \
    build-essential \
    curl \
    git \
    python3-venv \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

ENV CYFRIN_DIR=/opt/cyfrin
ENV PIPX_HOME=/opt/pipx
ENV PIPX_BIN_DIR=/opt/pipx/bin
ENV PATH="${PIPX_BIN_DIR}:/opt/cyfrin/bin:/root/.cargo/bin:/usr/local/bin:${PATH}"

# Ensure Cyfrin and pipx directories exist for tooling installs
RUN mkdir -p /opt/cyfrin/bin ${PIPX_HOME} ${PIPX_BIN_DIR}

# Set working directory
WORKDIR /app

# Copy requirements and install Python dependencies first for better caching
COPY requirements.txt ./
RUN pip3 install --no-cache-dir \
    --trusted-host pypi.org \
    --trusted-host pypi.python.org \
    --trusted-host files.pythonhosted.org \
    -r requirements.txt && \
    rm -rf ~/.cache/pip

# Install pipx to isolate CLI tooling environments
RUN pip3 install --no-cache-dir \
    --trusted-host pypi.org \
    --trusted-host pypi.python.org \
    --trusted-host files.pythonhosted.org \
    pipx \
    && rm -rf ~/.cache/pip

# Install Slither in an isolated environment via pipx to avoid dependency conflicts
RUN pipx install --pip-args="--no-cache-dir --trusted-host pypi.org --trusted-host pypi.python.org --trusted-host files.pythonhosted.org" slither-analyzer && \
    ln -sf ${PIPX_BIN_DIR}/slither /usr/local/bin/slither

# Install solc-select so the engine can align solc with each contract's pragma
RUN pip3 install --no-cache-dir \
    --trusted-host pypi.org \
    --trusted-host pypi.python.org \
    --trusted-host files.pythonhosted.org \
    solc-select && rm -rf ~/.cache/pip



# Copy source code
COPY farofino_mcp ./farofino_mcp
COPY examples ./examples
COPY pyproject.toml ./
COPY setup.py ./

# Install the package
RUN pip3 install --no-cache-dir \
    --trusted-host pypi.org \
    --trusted-host pypi.python.org \
    --trusted-host files.pythonhosted.org \
    -e . && rm -rf ~/.cache/pip

# Install Cyfrinup and provision the latest Aderyn build
RUN curl -LsSf https://raw.githubusercontent.com/Cyfrin/up/main/install | bash && \
    CYFRINUP_ONLY_INSTALL=aderyn cyfrinup && \
    install -Dm755 /root/.cargo/bin/aderyn /opt/cyfrin/bin/aderyn && \
    if [ -f /root/.cargo/bin/aderyn-update ]; then install -Dm755 /root/.cargo/bin/aderyn-update /opt/cyfrin/bin/aderyn-update; else true; fi && \
    aderyn --version >/dev/null

# Verify critical tooling is available
RUN slither --version && aderyn --version

# Create a non-root user (or use existing user if UID 1000 exists)
RUN id -u 1000 >/dev/null 2>&1 || useradd -m -u 1000 mcp && \
    chown -R $(id -un 1000):$(id -gn 1000) /app /opt/cyfrin

# Switch to non-root user (UID 1000)
USER 1000

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PATH="/opt/cyfrin/bin:/root/.cargo/bin:/usr/local/bin:/home/mcp/.local/bin:${PATH}"

# Expose stdio for MCP communication
ENTRYPOINT ["python3", "-m", "farofino_mcp"]

# Note: Slither is always available
# Aderyn may not be available if Cyfrinup installation fails
# Check available tools with the check_tools command
