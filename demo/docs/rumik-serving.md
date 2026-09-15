# Rumik serving contract

This contract records the pinned Rumik source inspected on 12 September 2026:
`rumik-ai/rumik-oss-1` at commit
`0ed3c98684e14350c910129c0efa179841c41ad2`. The inspected server source is
[server.py at that revision](https://huggingface.co/rumik-ai/rumik-oss-1/raw/0ed3c98684e14350c910129c0efa179841c41ad2/server.py).
It is a source inspection record, not a live deployment or latency result.

## HTTP contract

The server exposes `POST /v1/audio/speech`. Its `SpeechRequest` requires a
nonempty `input` string and accepts `speaker="Ira"`, `temperature=0.8`,
`top_k=30`, and `max_new_tokens=2048`. The response is a complete WAV containing
mono signed 16-bit PCM at 24 kHz. The server does not stream audio.

Observed error behavior:

| Condition | Response |
| --- | --- |
| Missing or empty `input` | HTTP 422 from Pydantic validation. |
| Unknown speaker | HTTP 400 JSON detail after `ValueError` (source-derived). |
| Synthesis `RuntimeError` | HTTP 400 JSON detail (source-derived). |
| Other server exception | HTTP 500 (source-derived). |

The server has no authentication or TLS contract. Its `/health` endpoint returns
static status/model information and does not report the pinned revision. Keep it
on a private loopback interface. A runnable server uses the downloaded full local
model snapshot, not the source-only directory. The snapshot has 60 files and is
about 6.7 GiB. Its three weight files are SHA-256 verified in
[`demo/rumik/download-manifest.json`](../rumik/download-manifest.json).

```bash
demo/.venv-rumik/bin/python demo/scripts/download_rumik.py
```

On the confirmed local host (Apple Silicon Mac, 48 GB unified memory, no external
GPU), use the MPS wrapper after the full snapshot is present:

```bash
demo/.venv-rumik/bin/python -m demo.rumik.mac_server \
  --device mps --dtype float32 --timeout-seconds 600
```

Install the Mac environment with `demo/requirements-rumik-mac.txt` in
`demo/.venv-rumik`, record resolved runtime versions, and verify the server hash
before starting it. The MPS smoke check and live server request have passed on
this Mac. The reference requirements use version ranges; captured runtime
versions are recorded in `demo/rumik/host-mac.json`.

## Concurrency and cancellation

The reference engine places synchronous `synthesize` work inside
`asyncio.to_thread` and protects it with one `asyncio.Lock`. This serializes
synthesis for one server process but leaves waiters unbounded. Canceling an
awaiting request can release the lock while its worker thread continues running;
there is no remote cancellation API. There is no streaming contract.

The application admission layer must therefore enforce one gate per server
process: one active request, at most two queued requests, and immediate overload
for further work. Quarantine unknown remote responses and reject audio whose
request ownership does not match the active gate. All clients sharing a process
must use that process gate; do not create multiple gates or rely on a global gate
claim. The next TTS adapter loop owns generation tagging and stale-audio rejection.

Cancelling a caller that is already active discards its result but retains the
transport slot until the response or deadline. On timeout the client cancels its
local HTTP task and permanently quarantines the gate. Local shutdown is bounded;
it does not prove that GPU work stopped. Verify completion or restart the server
before creating a replacement gate. HTTP headers and body share one total deadline.

## Snapshot and licensing

The source-only files currently captured under
`demo/models/rumik-source/0ed3c98684e14350c910129c0efa179841c41ad2/` are not
runnable model weights. The full local snapshot is
`demo/models/rumik-oss-1/0ed3c98684e14350c910129c0efa179841c41ad2/`, containing
the pinned `server.py`, `README.md`, `config.json`,
`codec/preprocessor_config.json`, requirement files, `LICENSE`, and `NOTICE`.
The captured hashes and provenance are recorded in [`demo/rumik/manifest.json`](../rumik/manifest.json)
and the downloaded weight hashes in [`demo/rumik/download-manifest.json`](../rumik/download-manifest.json).
The model card states CC BY-NC 4.0 with an acceptable-use addendum. Commercial
products and commercial self-hosting require separate permission. This is a
deployment review requirement and not legal advice.

The owner selected this local Mac workflow. Source files and full model weights
were downloaded; the model checkpoint and intended practice use remain recorded
in the manifest.

## Benchmark harness contract

The landed harness is `demo/rumik/benchmark.py`. It supports an offline
fixture mode and a live mode that requires an explicit private URL, host metadata,
and an exclusive-server declaration. Offline fixtures can validate request fields,
WAV decoding, error mapping, queue admission, and stale-result quarantine without
claiming provider behavior.

The completed report is [`demo/rumik/results/mac-float32.json`](../rumik/results/mac-float32.json):
one warmup and four successful speech requests, plus invalid-speaker and
missing/empty-input cases. It records queue wait, complete-request latency, WAV
duration, status/error, revisions, and host metadata. Complete-request latency
includes synthesis, encoding, and transport; it is not isolated GPU synthesis
time or audible latency. Warmups are separate from measured cases. If host memory is unavailable, record
`null`; never infer memory from model size or process configuration. Host/runtime
metadata is captured in [`demo/rumik/host-mac.json`](../rumik/host-mac.json), and
saved WAV paths are included in the report. Measured speech request times were
22.875, 27.716, 24.847, and 34.810 seconds for 2.48, 2.96, 2.64, and 3.60
seconds of audio; client queue wait reached about 34.8 seconds. These are serving
measurements, not realtime interview performance.

The reproducible commands are:

```bash
uv pip install --python demo/.venv-rumik/bin/python -r demo/requirements-rumik-mac.txt
demo/.venv-rumik/bin/python -m demo.rumik.benchmark --mode fixture \
  --request-timeout-seconds 600 --save-wav \
  --output demo/rumik/results/fixture.json
demo/.venv-rumik/bin/python -m demo.rumik.benchmark --mode live \
  --endpoint http://127.0.0.1:6006/v1/audio/speech \
  --request-timeout-seconds 600 --save-wav \
  --host-metadata demo/rumik/host.json --exclusive-server \
  --output demo/rumik/results/live.json
```

The exact `--request-timeout-seconds 600` and `--save-wav` options are part of the
Mac benchmark workflow. Fixture and live benchmark execution are complete. This
does not demonstrate the design’s five-second reply target, realtime performance,
browser integration, or a full interview.

Copy [host.example.json](../rumik/host.example.json) to `demo/rumik/host.json`
and replace its placeholders with facts from this Mac. Live validation requires
the device name, MPS/device and dtype selection, a nonempty
`provenance.attestation`, explicit `installed_versions`, and model/server
revisions plus the server SHA-256 matching the manifest. CUDA fields are not
required for this host. Host attestations are recorded, not remotely verified by
`/health`.

Optional memory data requires `measured: true`, a finite nonnegative `value`,
`units` (`bytes`, `MiB`, or `GiB`), `scope`, `device`, `source`, and
`captured_at_utc`. It is labelled an operator-reported measurement; incomplete
memory data becomes unavailable. Reported memory must come from the inference
host, not the benchmark client's process.

## Handoff boundary

Loop 06 fixtures and harness logic are ready on the local Mac. Loop 07 remains
blocked on model download and live audio verification. The Rumik Pipecat adapter must decode the WAV,
preserve 24 kHz or explicitly resample, bound admission, tag every request with
the application response generation, and drop late audio after interruption.
