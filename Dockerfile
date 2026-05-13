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
COPY --from=client-build /app/client/dist ./static

RUN chmod +x ./entrypoint.sh

EXPOSE 5001

CMD ["./entrypoint.sh"]
