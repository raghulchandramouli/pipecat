# Isolated Hugging Face speech-to-speech experiment

This local experiment pins `huggingface/speech-to-speech` at
`16d7f98ff712fb082d53497937f5456667c84680`, uses only `127.0.0.1:8765`, and
does not change Pipecat's root or `demo` environments. The launcher extends the
pinned project's in-memory `STT_BACKENDS` and `TTS_BACKENDS` registries before
it imports `s2s_pipeline`; the source under `HFS/upstream` is never patched.

## Bootstrap

From the Pipecat repository root:

```bash
python HFS/bootstrap.py
```

The command clones the exact revision, synchronizes only `HFS/.venv`, and
archives the generated upstream lock at `HFS/dependency-lock.toml`. It refuses
to overwrite an existing checkout. If the checkout is already at the pin:

```bash
python HFS/bootstrap.py --reuse-pinned
```

`HFS/upstream`, `HFS/.venv`, and the cache are ignored. Bootstrap never removes
an existing directory, including a failed partial checkout.

## Sarvam and Gemini launcher

Put credentials in `demo/.env` or in the server environment. The launcher loads
`demo/.env` only when it exists; environment variables take precedence. Use
`--env-file path/to/file` to select another dotenv file.

```dotenv
SARVAM_API_KEY=...
GOOGLE_API_KEY=...
```

Preflight validates credentials and port availability before importing models,
contacting a provider, or opening a microphone:

```bash
env -u PYTHONPATH HFS/.venv/bin/python -m HFS.speech_to_speech serve --check
```

Start the server:

```bash
env -u PYTHONPATH HFS/.venv/bin/python -m HFS.speech_to_speech serve
```

Start the server plus upstream's loopback microphone/speaker client:

```bash
env -u PYTHONPATH HFS/.venv/bin/python -m HFS.speech_to_speech local
```

The default profile selects native `sarvam` STT/TTS, `chat-completions`, model
`gemini-3.8-flash`, and low reasoning effort. The pinned source's generic
chat-completions backend has model, API-key, base-URL, stream, and reasoning
arguments, constructs `OpenAI(api_key=..., base_url=...)`, and forwards
`reasoning_effort` in the provider body. The launcher therefore configures the
Google [OpenAI-compatible endpoint](https://ai.google.dev/gemini-api/docs/openai)
using `GOOGLE_API_KEY` server-side. Google's documented `reasoning_effort=low`
maps to Gemini's low thinking level. It sets
the generic handler's `OPENAI_API_KEY` fallback only when that variable is not
already present. Sarvam stays in `SARVAM_API_KEY` and is sent only as the native
Sarvam subscription credential, never as an OpenAI bearer key.

This source inspection establishes configuration compatibility. It does not
prove a live key, model availability, provider acceptance, latency, or audio
quality. Run provider-backed qualification before treating those as evidence.

Choose a positive local port in `1..65535`:

```bash
env -u PYTHONPATH HFS/.venv/bin/python -m HFS.speech_to_speech serve --port 8766
```

The launcher accepts only `127.0.0.1` and at most four pipelines. `Ctrl-C`
delegates graceful shutdown to upstream's owned handler manager. The launcher
only probes a port and never kills another process.

## Supported upstream baseline

To use a pinned upstream baseline without Sarvam, delegate backend selection to
the upstream project. Its documented Apple Silicon preset is available as:

```bash
env -u PYTHONPATH HFS/.venv/bin/python -m HFS.speech_to_speech local --profile upstream --mac-optimal-settings
```

The baseline can download models and open a microphone. It is installation
evidence only, not a Sarvam comparison result.

## Qualification

The failure requirements and T11 isolation acceptance criteria are in
[failure-recovery/plan.md](failure-recovery/plan.md) and
[failure-recovery/test-plan.md](failure-recovery/test-plan.md). A passing
preflight does not prove provider calls, playback, cancellation, interruptions,
or interview behavior.

Run the isolated tests without inheriting Pipecat's root pytest configuration:

```bash
HFS/.venv/bin/python -m pytest --confcutdir=HFS -c HFS/pytest.ini HFS/tests -q
```
