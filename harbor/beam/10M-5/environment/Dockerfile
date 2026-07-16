FROM python:3.11-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY chat.json /app/chat.json
COPY questions.json /app/questions.json

