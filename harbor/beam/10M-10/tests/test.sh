#!/usr/bin/env bash
set -euo pipefail

ANSWERS="${1:-/app/answers.json}"
OUT="${2:-/logs/verifier/scores.json}"
REWARD="${3:-/logs/verifier/reward.txt}"

exec python /tests/beam_judge.py \
    --questions /tests/probing_questions.json \
    --answers "$ANSWERS" \
    --out "$OUT" \
    --reward-file "$REWARD"
