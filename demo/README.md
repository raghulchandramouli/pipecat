# Conversational interview demo

Status: browser and eval entry points wire isolated interview sessions through
Sarvam STT, local VAD, Gemini, strict transcript/reply validation, and Sarvam TTS.
See [browser setup and evidence](docs/browser-interview.md). Physical microphone,
echo, and audible interruption acceptance remain separate live checks.

The design source is [`docs/architecture/conversational-interview-mock.md`](../docs/architecture/conversational-interview-mock.md).
The application contract, open decisions, event flow, ownership boundaries, and
planned paths are in [`demo/docs/contracts.md`](docs/contracts.md).

The implementation is intentionally rooted at `demo/`. From the repository root,
create the isolated demo environment and run the smoke entry point with:

```bash
uv venv demo/.venv --python 3.12
uv pip install --python demo/.venv/bin/python -r demo/requirements-dev.txt
demo/.venv/bin/python -m demo
```

The smoke command validates an offline sample configuration; it makes no provider
calls and downloads no model artifacts.

Run the focused checks from the repository root:

```bash
demo/.venv/bin/python -m pytest -c demo/pytest.ini demo/tests
demo/.venv/bin/ruff check demo
demo/.venv/bin/ruff format --check demo
```

Live provider evidence is recorded separately from deterministic tests in the
[browser guide](docs/browser-interview.md) and the speech synthesis results below.

## Sarvam event extension

See [segment events and setup](docs/sarvam-events.md) for the loop 02 extension,
explicit boundary/utterance binding, and tests against the real framework. Its
additional dependencies are isolated in `demo/.venv`; the offline foundation smoke
command above still needs only the foundation dependencies.

## Transcript ledger and dispatch gate

See [transcript gate setup and contracts](docs/transcript-gate.md) for canonical
transcript snapshots, managed final deadlines, Gemini admission, and cancellation.
Provider correlation remains explicit; unresolved transcript state blocks inference.
See [turn policy and reply authorization](docs/turn-policy.md) for coordinated
pauses, thinking time, strict streamed-reply validation, and composition.
See [interview controller](docs/controller.md) for question progression, exact voice
controls, staged answer acceptance, and evidence validation.

## Run a scripted interview

Install the service dependencies, then run the two-question example from the
repository root:

```bash
uv pip install --python demo/.venv/bin/python -r demo/requirements-gate.txt
PYTHONPATH=src demo/.venv/bin/python -m demo.scripted_interview
```

It supplies finalized transcript fixtures and synthetic model replies through the
managed session and reply guard, prints the spoken conversation, and checks that
four answers (two initial answers and two follow-up answers) are accepted before
closing. It needs no microphone, credentials, network requests, or TTS model.

For a provider-backed composition, use `InterviewSession.create_llm(api_key=...)`,
`create_user_aggregator()`, and `create_reply_guard()`. Keep the guard before TTS and
output; bind Sarvam utterances to verified local boundaries before accepting finals.
Sarvam TTS is connected through the session factory used by both browser and eval entry points.

## Sarvam speech output

The active TTS provider is **Sarvam Bulbul v3**, with the `shubh` voice and streamed
24-kHz PCM. Use `session.create_tts(api_key=...)` after the reply guard. It also
reads `SARVAM_API_KEY` from the environment; the smoke command loads `demo/.env`.

```bash
uv pip install --python demo/.venv/bin/python -r demo/requirements-tts.txt
demo/.venv/bin/python -m demo.scripts.smoke_sarvam_tts --text 'Thank you. What did you change first?'
```

See [setup and interruption behavior](docs/sarvam-tts.md),
[the live measurement](sarvam/results/adapter-smoke.json), and
[generated audio](sarvam/results/adapter-smoke.wav).
The demo does not need the local Rumik server. Historical Rumik serving and
benchmark instructions remain in [the Rumik contract](docs/rumik-serving.md).
Downloaded weights remain in the ignored root `models/` directory.
