# Rumik TTS adapter contract

This document defines loop 07’s adapter boundary between the guarded interview
reply and the private Rumik HTTP server. The serving contract and measured Mac
evidence are in [rumik-serving.md](rumik-serving.md); reply authorization is in
[controller.md](controller.md) and [turn-policy.md](turn-policy.md).

## Ownership and placement

`RumikTTSService` accepts only a complete matching response envelope released by
the interview reply guard: one `LLMFullResponseStartFrame`, exactly one approved
`LLMTextFrame`, and one matching `LLMFullResponseEndFrame`. Bare text and partial
or duplicate envelopes never start HTTP work. Its
constructor takes `current_playback_epoch`, an endpoint (defaulting to
`http://127.0.0.1:6006/v1/audio/speech`), `speaker="Ira"`, `queue_capacity=2`,
`timeout=600.0`, and an injectable `RumikHTTPResponse` transport for tests. Each
request carries the application response generation and Pipecat `context_id`.
Rumik owns synthesis; the adapter owns request admission, generation matching,
WAV validation, PCM conversion, lifecycle frames, cancellation bookkeeping, and
nonfatal error reporting. A response from an obsolete generation is discarded
before any `TTSAudioRawFrame` is emitted.

`InterviewSession.create_tts()` caches one adapter instance for the session and
passes its playback-epoch callback. The pipeline boundary is:

```text
Gemini or deterministic controller prompt
  -> ReplyGuardProcessor
  -> Rumik TTS adapter
  -> TTS lifecycle/audio frames
  -> output transport
```

The guard remains before TTS and output. A frame forwarded downstream is an
application release signal; it does not prove that audio was heard or played.

## Request and audio contract

The adapter sends `POST /v1/audio/speech` to the private endpoint with the approved
spoken text and configured speaker. The guard removes completion markers and extracts approved spoken coaching text.
The adapter rejects residual markers, structured payloads, and empty text, and keeps
credentials and private endpoint configuration server-side. The pinned server
returns a complete WAV: mono, signed 16-bit PCM, 24 kHz. The adapter validates the
container, channels, sample width, sample rate, and nonempty payload before
emitting `TTSStartedFrame`, `TTSAudioRawFrame`, and `TTSStoppedFrame` in the
current Pipecat context contract. Malformed, empty, unsupported, or non-2xx
responses emit a nonfatal pipeline error and no audio.

Rumik has no word timestamps, streaming response, or remote cancellation API. The
adapter therefore cannot identify the exact spoken word at an interruption. It can
cancel local work, clear queued output where the transport supports it, and reject
late audio; already buffered or audible client audio may continue until the
transport stops it. Measure audible stop time at the client separately.

## Admission and lifecycle

Each adapter instance owns one bounded single-flight queue: one active synthesis
request and at most two queued requests. Further requests fail immediately as
overload. The Mac server separately enforces a process-wide single-flight gate;
the adapter queue does not provide cross-instance protection. Construct one
adapter for that server process and preserve its exclusivity. Unknown remote
completions are quarantined until the server is verified or restarted.

Queued work is cancellable before dispatch. Active HTTP cancellation discards its
result, but remote computation may continue; the local slot remains occupied until
completion or the 600-second request deadline. A timeout quarantines admission. After verifying server completion or restarting it,
call `recover_after_server_restart()`; it refuses recovery while any local HTTP task
is still active.

On interruption or speech resumption, the session advances its playback epoch;
the adapter invalidates local work and rejects any late result before frame
emission. A graceful `EndFrame` drains already admitted output before forwarding
the end frame. `CancelFrame` and `StopFrame` discard pending output immediately.
Cleanup cancels managed tasks and leaves no late audio eligible.
The adapter must use tracked Pipecat tasks and never block `process_frame` on
HTTP or synthesis.

## Errors and metrics

Map HTTP validation (422), invalid speaker/runtime failures (400), server failures
(500), timeout, overload, quarantine, malformed WAV, cancellation, and shutdown to
diagnostic categories without treating them as accepted interview content. Errors
are nonfatal unless the enclosing application explicitly decides the session cannot
recover.

`on_rumik_metrics` reports queue wait, complete request duration, and generated
audio duration. Record these with response generation, context ID, model/server
revision, and endpoint provenance. Request duration includes transport and complete response;
it is not GPU-only time or audible latency. Keep metrics separate from transcript
or coaching evidence.

## Verification evidence

Deterministic adapter tests cover valid WAV, malformed/empty WAV, non-2xx,
timeout, overload, cancellation, stale completion, and shutdown without task leaks.
The live adapter smoke report is [`demo/rumik/results/adapter-smoke.json`](../rumik/results/adapter-smoke.json)
and its saved WAV is [`demo/rumik/results/adapter-smoke.wav`](../rumik/results/adapter-smoke.wav).
It produced the complete lifecycle `TTSStartedFrame`, `TTSAudioRawFrame`,
`TTSTextFrame`, `TTSStoppedFrame`, with a 1.36-second WAV and 13.50-second
request duration on the private Mac server. The report records worker cleanup.
This is a short-sentence serving smoke, not browser audio acceptance, realtime
performance, or the design’s five-second reply target; the full interview remains
outside this loop.
