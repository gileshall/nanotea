#!/bin/sh
# Print the transcript of an audio file using openai-whisper and its cached models (~/.cache/whisper).
# usage: transcribe PYTHON MODEL AUDIO_FILE
set -eu
out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT
"$1" -m whisper "$3" --model "$2" --language en --fp16 False --verbose False \
    --output_format txt --output_dir "$out" >&2
cat "$out"/*.txt
