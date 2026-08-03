FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY db ./db
COPY static ./static
COPY scripts ./scripts

ENV PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install --no-cache-dir --root-user-action=ignore .

ENV PYTHONUNBUFFERED=1
