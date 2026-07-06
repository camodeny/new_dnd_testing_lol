# syntax=docker/dockerfile:1.5

FROM node:20-alpine AS client-build

WORKDIR /app/client

COPY client/package.json client/package-lock.json ./
RUN npm ci

COPY client/ ./
RUN npm run build

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=5001

WORKDIR /app/server

COPY server/requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir -r requirements.txt

COPY server/ ./
COPY automation/ /app/automation/
COPY --from=client-build /app/client/dist ./static

RUN chmod +x ./entrypoint.sh /app/automation/*.sh /app/automation/ensure_host_audit_env.py \
    && ln -sf /app/automation/automationctl.sh /usr/local/bin/dnd-automationctl \
    && ln -sf /app/automation/ensure_host_audit_env.py /usr/local/bin/dnd-ensure-host-audit-env

EXPOSE 5001

CMD ["./entrypoint.sh"]
