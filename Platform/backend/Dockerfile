FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml /app/pyproject.toml
COPY services /app/services
COPY shared /app/shared
COPY alembic /app/alembic
COPY alembic.ini /app/alembic.ini
COPY ansible.cfg ansible-requirements-roles.yml ansible-requirements-collections.yml /app/

ARG KUBECTL_VERSION=v1.29.6
ARG INSTALL_SYSTEM_DEPS=0
RUN if [ "$INSTALL_SYSTEM_DEPS" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends openssh-client git curl ca-certificates \
      && curl -fsSL -o /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
      && chmod +x /usr/local/bin/kubectl \
      && rm -rf /var/lib/apt/lists/*; \
    fi

RUN pip install --no-cache-dir -e .

# Chromium for browser-tool-service (Playwright) — optional, only where needed.
ARG INSTALL_PLAYWRIGHT=0
ARG PLAYWRIGHT_INSTALL_DEPS=0
RUN if [ "$INSTALL_PLAYWRIGHT" = "1" ]; then \
      if [ "$PLAYWRIGHT_INSTALL_DEPS" = "1" ]; then \
        python -m playwright install --with-deps chromium; \
      else \
        python -m playwright install chromium; \
      fi; \
    fi

# Ansible Galaxy roles/collections for prepare-stack (robertdebock.gitlab, cloudkrafter.nexus).
# ANSIBLE_CONFIG before RUN — so ansible-galaxy reads [galaxy] timeout from ansible.cfg.
# Retries: timeouts/disconnects when downloading artifacts from galaxy.ansible.com during docker build.
ENV ANSIBLE_CONFIG=/app/ansible.cfg
ARG INSTALL_ANSIBLE_GALAXY=0
RUN if [ "$INSTALL_ANSIBLE_GALAXY" = "1" ]; then \
      mkdir -p /app/ansible/roles /app/ansible/collections \
      && ansible-galaxy role install -r /app/ansible-requirements-roles.yml -p /app/ansible/roles \
      && for attempt in 1 2 3 4 5; do \
           ansible-galaxy collection install --timeout 300 -r /app/ansible-requirements-collections.yml -p /app/ansible/collections && break; \
           if [ "$attempt" -eq 5 ]; then exit 1; fi; \
           echo "ansible-galaxy collection install failed (attempt $attempt), retry in 30s..."; \
           sleep 30; \
         done; \
    fi

ENV PYTHONUNBUFFERED=1
