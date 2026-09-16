# Interaction acceptance

The browser default is Gemini 3.8 Live native speech-to-speech. Tanglish is the
default style and is requested as slow, clear conversation guidance; this is a
qualitative instruction, not a numeric speech-rate guarantee. Language preferences and speaking style are selected before connecting. The legacy
Sarvam STT/TTS pipeline remains available to its separate eval entry point.

This runbook covers the controls and recovery behavior in the interview. The
fixtures under `demo/evals/interaction/` cover ordinary answers, hesitation and
resumed speech, clarification, no-example answers, mixed answer/help turns,
interruption, explicit controls, and English/Tanglish/Hinglish. They assert
semantic events; generated text or a guard event does not prove that a person
heard audio.

## Prepare

From the repository root, the deterministic foundation is:

```bash
uv venv demo/.venv --python 3.12
uv pip install --python demo/.venv/bin/python -r demo/requirements-gate.txt
PYTHONPATH=src demo/.venv/bin/python -m pytest -c demo/pytest.ini demo/tests/test_interaction_scenarios.py -q
PYTHONPATH=src demo/.venv/bin/python demo/scripts/check_interaction_acceptance.py
```

For browser and provider work, prepare the built package and separate test
dependencies:

```bash
python3 demo/scripts/prepare_pipecat.py
uv pip install --python demo/.venv/bin/python 'demo/.build/pipecat[google]' -r demo/requirements-browser.txt
uv pip install --python demo/.venv/bin/python -r demo/requirements-browser-test.txt
```

`requirements-gate.txt` covers offline policy/controller checks. The browser
requirements add Sarvam/Gemini integration and Playwright; they supply no
credentials and make no provider calls. The focused browser gate must have zero
skips:

```bash
PYTHONPATH=src demo/.venv/bin/python demo/scripts/check_interaction_acceptance.py --frontend
```

## Exercise native Gemini audio

The native browser smoke checks opening audio, Explain, thinking time, a split
candidate answer with a 1.5-second pause, Repeat, Skip, and End. It records
received WebRTC audio bytes, captions, errors, and screenshots:

```bash
PYTHONPATH=src demo/.venv/bin/python -m demo.scripts.smoke_gemini_conversation --url http://127.0.0.1:7871 --language tanglish --candidate path/to/candidate.wav
```

Native audio is generated before its transcript is available. The Sarvam path's
pre-speech evidence and structured-response gate assertions do not apply to this
mode. Listening tests are still needed to assess pace and language naturalness.

### Recorded native run — 2026-09-16

`demo/browser/results/gemini-conversation/20260916T084417Z/report.json` passed
opening audio, Explain, silent thinking time, a split Tamil answer with a
1.5-second pause, Repeat, Skip, and End. Chromium received 669,421 audio bytes
and reported no page errors or application notices. This was a synthetic
microphone run against real Gemini 3.8 Live, not a human assessment of voice pace.

Casual conversation first asks the person's occupation or daily role, then
follows their answers with questions relevant to what they actually do.
It moves between topics without requiring Skip. The conversation panel shows
Gemini's actual wording without a fixed question count.

### Recorded casual conversation — 2026-09-16

`demo/browser/results/gemini-conversation/20260916T090959Z/report.json` passed
with two recorded candidate answers. The opening was “Vanakkam! Inaiku unga day
eppadi pochu?” After the teamwork answer Gemini asked about the person's feelings;
after a no-experience answer it moved to what the person enjoys, without Skip.
Repeat, New topic, thinking time, the 1.5-second input pause, and End passed.
The browser received 321,120 audio bytes with no page errors or notices. The
question counter stayed hidden and the conversation panel matched spoken text.

## Exercise legacy Sarvam paths

Put credentials in `demo/.env` or the server environment. Start a fresh eval bot
for each scenario so question and transcript state cannot contaminate the next:

```bash
# Start the legacy eval bot as documented in demo/README.md.
PYTHONPATH=src demo/.venv/bin/python -m pipecat.evals run demo/evals/interaction/hesitation.yaml --bot-url ws://127.0.0.1:7861 -v
```

For the legacy browser smoke, inject LiveInterviewPipelineFactory into the server
application and use an English synthetic candidate. The
report records event ordering and timing fields; it does not claim audible
latency or naturalness:

```bash
# Requires a server explicitly configured with the legacy factory.
PYTHONPATH=src demo/.venv/bin/python -m demo.scripts.smoke_interaction --url http://127.0.0.1:7871 --language english --candidate demo/browser/results/candidate/adapter-smoke.wav
```

If that recording is unavailable, provide another uncompressed 16-bit PCM WAV
through `--candidate`; a missing recording is not a pass. Preserve
server speech-stop, `finalReady`, dispatch, reply-guard authorization, playback
start, and actual playback-stop as separate timestamps.

The `pipecat.evals` command above is the retained legacy Sarvam/EvalTransport
path. It is useful for semantic fixtures, but does not represent the default
Gemini native browser audio path.

## Conversation contract

The user can say or press `repeat`, `explain`, `give me a moment`, `skip`, and
`end`. Repeat restates the active question. Explain gives a concise
interpretation; if unavailable, the active question is preserved and repeat is
offered. Give-me-a-moment preserves pending transcript and suppresses a competing
check-in. Skip closes the current question without grading pending text. End
closes the interview.

An ordinary answer advances only after a trusted complete transcript and a
guard-approved response. Hesitation or an incomplete thought remains pending;
resumed speech joins that candidate turn. Mixed answer/help preserves the answer
while guidance is given. A missing or ambiguous final is held for transcript
retry/replacement and cannot acknowledge or advance the answer.

Provider or response failures get at most two attempts on the same question and
transcript revision. After the second failure, recovery guidance offers repeat,
skip, or end; stale output is discarded. A lost connection is not restored in
this loop: show connection-lost and start a new session.

## Human evidence

An owner plus two additional speakers should repeat each task in every offered
language. Record naturalness and control from 1–5, accidental advances, and
recovery success. Acceptance requires at least 4/5 for both ratings and zero
accidental advances. Report p95 audible latency only with at least 30 samples
per language and condition; otherwise mark `NOT RUN`.

```text
Date / build:
Speaker / language / device:
Scenario / condition:
Naturalness (1–5): NOT RUN
Control (1–5): NOT RUN
Accidental advance: NOT RUN
Recovery succeeded: NOT RUN
Boundary timestamps (30+ samples): NOT RUN
P95 audible latency: NOT RUN
Browser errors / skipped tests: NOT RUN
Notes / recording paths:
```
