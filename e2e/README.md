# End-to-end runs

nanotea deployed the ways [docs/running.md](../docs/running.md) and [docs/service.md](../docs/service.md)
describe, with open-source voices, transcription and rewriting, then driven headless: the owner in Chromium,
agents over MCP and `nanotea-tell`. Nothing reaches a real push service or a paid model.

| scenario | where | voice | transcription | rewriting | in front |
|---|---|---|---|---|---|
| `native-macos` | this Mac | `say` (picked by `nanotea init`) | whisper (`--whisper-model`) | `llm` + Ollama, qwen2.5:0.5b | nothing: `http://127.0.0.1` |
| `native-linux` | the driver container as a Linux box | espeak-ng (picked by `nanotea init`) | whisper base.en | `llm` + Ollama, qwen2.5:0.5b | nothing: `http://127.0.0.1` |
| `docker` | the shipped `compose.yaml` | Kokoro-FastAPI (`--profile kokoro`) | whisper small | identity | Caddy on the host, `tls internal` |
| `docker-kokoro` | the image built with `NANOTEA_KOKORO=1` | Kokoro in the service | whisper small | identity | Traefik, then nginx (`deploy/traefik`) |

## Running

```bash
python3 e2e/run.py linux                     # the three Linux scenarios, on a machine with docker
e2e/remote.sh me@dockerhost linux            # the same on another machine over ssh; results in e2e-out/
python3 e2e/run.py native-macos --whisper-python ~/.venvs/whisper/bin/python3
```

`native-macos` needs uv, ffmpeg, Ollama with `qwen2.5:0.5b` pulled, and a python with openai-whisper; it installs
nanotea and `llm` into the run's own directory. The Linux scenarios need python3 and docker with buildx. The
workflow `.github/workflows/e2e.yml` runs every scenario on GitHub's runners.

## What a run checks

`journey.py`, in order, stopping at the first failure:

1. native only: `uv tool install`, `nanotea init`, the config edits as the Configuration page makes them,
   `nanotea config check`, `nanotea plugins --check`, `nanotea serve`
2. an unpaired browser gets the not-paired page; the pairing link pairs it, with a Secure cookie over https
3. an agent with no token asks for one; the owner approves it under Tokens; it joins with a free voice
4. the owner makes it the approver; it approves a second agent (`nanotea-tell`) through `decide`
5. a message is rewritten and delivered; the owner plays it, and whisper transcribes the voice it got
6. the owner taps a choice; records an answer through Chromium's fake microphone; types a message
7. notifications: the page subscribes with the run's own push service (`pushmock.py`), which decrypts each push
   and checks its VAPID signature; the service worker shows the push it is given
8. an event source asks for an events token and the approver gives it; the event reaches the agent
9. the owner revokes a token, and the agent using it is refused

`DIR/<scenario>/results.json` has each check, its time and its numbers; `artifacts/` has the voice, the recorded
answer, screenshots, the service's log and the browser's console. Word recall (the share of a sentence's words
whisper hears) and loudness say the audio carried the words; they are not a judgment of how it sounds.

## On a shared docker host

Everything is named `nanotea-ci*`: images, projects, the buildx builder (`nanotea-ci`, capped), and the cache
volumes `nanotea-ci-cache-service`, `-driver` and `-ollama`, which keep the models between runs. Each container is
capped and has a low CPU weight. Ports are on loopback, 27443 to 27480. Each scenario's containers and data volume
are removed when it ends (`--keep` leaves a failed one up). To remove the rest:

```bash
docker buildx rm nanotea-ci
docker volume rm nanotea-ci-cache-service nanotea-ci-cache-driver nanotea-ci-cache-ollama
docker image rm nanotea-ci-driver:latest nanotea-ci-service:whisper nanotea-ci-service:kokoro
```
