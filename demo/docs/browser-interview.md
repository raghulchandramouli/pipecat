# Browser interview

The local application uses Pipecat's SmallWebRTC transport, which connects browser
microphone and playback tracks directly to the Python pipeline without a separate
media service. The default browser conversation uses Gemini 3.8 Live native
speech-to-speech: microphone audio goes to Gemini and its native audio output is
played back while captions are rendered from the conversation events. The older
EvalTransport/RTVI entry point remains a legacy test path.

## Run

From the repository root:

```bash
uv venv demo/.venv --python 3.12
python3 demo/scripts/prepare_pipecat.py
uv pip install --python demo/.venv/bin/python 'demo/.build/pipecat[google]' -r demo/requirements-browser.txt
GOOGLE_API_KEY=... PYTHONPATH=src demo/.venv/bin/python -m demo.interview.server --host 127.0.0.1 --port 7871
```

Put `GOOGLE_API_KEY` in `demo/.env` or the server environment.
Startup rejects missing credentials without displaying their values. Open
<http://127.0.0.1:7871>, choose the language, role, difficulty, duration and rubric, then enable
microphone and playback. Speak naturally after the opening question; there is no
per-answer submit or stop-speaking control. Browser microphone access requires
localhost or HTTPS. This entry point is a local practice server.

The default language is slow Tanglish (Tamil mixed with English), expressed as
conversation guidance to Gemini rather than a numeric speech-rate guarantee. The
setup selector offers all 97 Gemini languages plus Tanglish and Hinglish. Say
“repeat the question,” “give me a moment,” “skip this question,” or “end the
interview.” Native Gemini audio and captions are part of the same live session;
provider failure is surfaced as a reconnect/error state.

## Conversation flow

The round first asks what work the person does or how they spend their days,
then waits for the answer. It adapts to their occupation or daily role without
assuming employment from the setup role. Gemini follows
concrete details from their answers, asks one question at a time, and moves to
another related topic after one or two follow-ups. Listening themes guide the
conversation privately; they are not a fixed question list. Brief answers and
lack of experience are accepted without pressure.

Skip remains an optional way to change the subject. Repeat and Explain refer
to the latest conversational question. The conversation panel shows Gemini's
actual wording, without a numbered question counter.

## Pipeline and ownership

Each session owns its conversation state, context, service instance, worker, and
transport. A long-lived `WorkerRunner` hosts the browser sessions.
The pipeline follows this order, with application event bridges between stages:

```text
transport input → Gemini 3.8 Live native conversation → native audio output/captions
→ browser playback and interview state
```

Gemini owns the native speech-to-speech input/output session. Provider credentials
and endpoints stay on the server;
client setup accepts only interview choices. Assistant aggregation records
conversation text alongside native audio turns.

Native Gemini audio events supply the conversation captions. They are displayed as
conversation state and do not use the older pre-speech JSON evidence/reply-guard
authorization path.

The microphone remains active during playback with echo cancellation requested.
Speech resumption invalidates server response/playback ownership, interrupts the
pipeline, and sends an interruption event to clear browser playback. Browser media
buffering and acoustic echo require separate measured audio acceptance.

## Public API and events

| Route | Purpose |
| --- | --- |
| `POST /api/sessions` | Validate role, difficulty, duration, rubric and language; return an opaque session ID and offer URL. |
| `POST /api/sessions/{id}/offer` | Negotiate the session's SDP offer. |
| `PATCH /api/sessions/{id}/ice` | Add ICE candidates for the bound peer connection. |
| `GET /api/sessions/{id}` | Return public status, question progress and caption state. |
| `DELETE /api/sessions/{id}` | Cancel the pipeline and release its peer connection. |

The data channel carries plain JSON defined in `browser_contract.py`. Server events
have `v: 1`, `session_id`, `connection_generation`, and a monotonically increasing
`seq`. Clients reject stale or foreign events. Client `interview.ready` starts the
opening only after media setup.

| Event | Meaning |
| --- | --- |
| `interview.status` | Listening, Giving you time, Thinking, Speaking, or Reconnecting, with optional question progress and playback epoch. |
| `interview.caption` | Provisional or finalized user transcript, identified by segment. Finalization does not itself accept an answer. |
| `interview.reply` | Native interviewer output transcript, identified by reply. |
| `interview.interruption` | Invalidate playback through the supplied epoch. |
| `interview.error` | A public, sanitized failure notice. |
| `interview.ended` | Interview completion and media teardown. |

`Reconnecting` indicates unavailable transport/transcription. Restoring accepted
state across a replacement connection is loop 09 work.

## Legacy eval and validation

```bash
PYTHONPATH=src demo/.venv/bin/python -m demo.interview.eval_bot --port 7861
# In another terminal, use an audio scenario against the shared bot logic:
PYTHONPATH=src demo/.venv/bin/python -m pipecat.evals run path/to/scenario.yaml --bot-url ws://localhost:7861
```

Use the repository's `pipecat eval run` entry point if installed. This legacy eval
path receives
guard-approved TTS text and audio; raw model text and internal coaching are not
forwarded as RTVI LLM events. This legacy path does not exercise the native
Gemini browser session. Provider-backed audio scenarios exercise STT and
turn-taking; a fixture or text-only scenario cannot establish microphone behavior.

Focused checks:

```bash
PYTHONPATH=src demo/.venv/bin/python -m pytest -c demo/pytest.ini demo/tests/test_browser_contract.py demo/tests/test_browser_pipeline.py demo/tests/test_browser_server.py demo/tests/test_browser_stt.py demo/tests/test_browser_eval.py -q
demo/.venv/bin/ruff check demo
demo/.venv/bin/ruff format --check demo
```

For browser automation, install `demo/requirements-browser-test.txt` and run
`demo/.venv/bin/playwright install chromium`. The smoke uses the included synthetic
candidate and end-command WAVs under `demo/browser/results/`. Pass `--chromium`
to select a different Chromium executable.

```bash
PYTHONPATH=src demo/.venv/bin/python -m demo.scripts.smoke_browser
PYTHONPATH=src demo/.venv/bin/python -m demo.scripts.smoke_browser --interrupt-opening
```

The live smoke harness in `demo/scripts/smoke_browser.py` uses Chromium, synthetic
microphone WAVs, real WebRTC, and live providers. Its result belongs under
`demo/browser/results/`. It does not establish physical microphone permission,
echo behavior, or audible interruption latency with speakers/headphones. See the
[loop 08 handoff](../../.loop/08-browser-and-pipeline.md) for current evidence.

Provider references checked on 2026-09-15:
[Gemini model and thinking settings](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash),
[Sarvam streaming TTS](https://docs.sarvam.ai/api-reference/text-to-speech/stream)
(legacy path only).

## Current evidence

On 2026-09-15, the [full interview](../browser/results/browser-smoke.json) and
[opening interruption](../browser/results/browser-interruption.json) passed
concurrently with synthetic microphone audio and live providers. The full interview
produced a grounded Hinglish follow-up and closed on the spoken end command. The
interruption run detached playback and closed independently. Both had zero browser
errors; captured events had distinct session IDs and no configured provider
credentials. [The validation summary](../browser/results/loop08-validation.json)
records test counts and source verification.

The [conversation screenshot](../browser/results/conversation.png) shows the
canonical transcript and approved response. Physical microphone, acoustic echo and
latency measurements remain unrun. Invalid model replies remain blocked and produce
a recoverable notice; these short successful runs do not establish a reliability rate.

The [interaction acceptance pack](interaction-acceptance.md) covers hesitation,
resumed speech, clarification, controls, and recovery. Its event assertions are
semantic; audible timing still requires boundary timestamps and human trials.
