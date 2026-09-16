# Developer experience review: failure recovery

Date: 2026-09-16. Mode: DX POLISH. Reviewer: independent Codex agent. Claude is unavailable; this report does not claim cross-model consensus. This document amends the proposed plan, not implemented behavior. Scores assess plan completeness, not a measured live experience.

## Persona and scope

The primary developer is the demo owner or a Python contributor diagnosing a failed voice interview on an Apple Silicon laptop. They already have repository access and expect existing Sarvam credentials to work, but should not need to understand every processor before identifying the failed stage. Target tolerance is five minutes to a useful diagnostic once dependencies and credentials exist; cold installation and provider signup are separate costs. The product surface is a local CLI/runbook plus a proposed integration experiment, not a new SDK or hosted platform.

## Developer perspective

I open `demo/README.md` and see “Conversational interview demo.” The status names Sarvam, VAD and Gemini, and points me to browser setup. The first runnable sequence creates `demo/.venv`, installs foundation requirements and runs `python -m demo`. The next paragraph correctly explains that this is offline, so I cannot use its success to decide whether my key works. I follow the browser guide and find another install sequence, including `prepare_pipecat.py` and a locally built package. I now understand why installing just the TTS requirements might leave other imports missing.

I want one speech sample before opening the browser. The TTS smoke accepts a language, text and output directory, and produces a WAV plus JSON. That feels useful because I can inspect the exact output. For realtime recognition I find a script, but it reads a fixed Hinglish WAV and writes fixed result names. I cannot pass my failing recording, and repeating a run could replace evidence. If it fails before completion, I may get a traceback instead of a saved diagnostic.

The isolated HF proposal gives me a safe place to experiment, but I need the instructions to tell me which commands exist now. I would lose confidence if “use Sarvam” meant guessing a flag. My first useful result is a saved, attributable audio round trip; a generic chatbot answering once would not prove that interview progression or interruptions work.

## Evidence and competitive benchmark

Read: `demo/README.md`, `demo/docs/browser-interview.md`, `demo/docs/sarvam-events.md`, `demo/scripts/smoke_sarvam_tts.py`, `demo/scripts/probe_sarvam_realtime.py`, `demo/interview/sarvam_tts.py`, and dependency files. Primary upstream documentation was checked online; no setup or provider call was executed by this review.

| Reference | Time to first result | Relevant choice | Implication |
|---|---|---|---|
| [Pipecat quickstart](https://docs.pipecat.ai/pipecat/get-started/quickstart) | Advertises under five minutes; not independently timed here | Explicit Python/uv and three-provider credential prerequisites | State prerequisite time separately from the runnable path |
| [HF speech-to-speech](https://github.com/huggingface/speech-to-speech) | Not measured; no verified setup duration used | Source install and backend/device configuration | Pin source and document the actual supported backend before Sarvam adaptation |
| [Sarvam HTTP streaming guide](https://docs.sarvam.ai/api/api-guides-tutorials/text-to-speech/streaming-api/http-stream) | Not measured here | A direct text request produces binary audio | Provider-only success should precede framework comparison |
| This recovery plan | Current cold time unknown; warm TTS diagnostic target 2–5 minutes | Existing smoke plus isolated experiment | Do not promise a complete HF Sarvam bot in five minutes |

The useful first moment is a WAV plus a readable manifest showing which stage succeeded, its exact configuration and the next command. Reuse the copy-paste terminal path; a hosted playground would add unrelated infrastructure and another place to handle credentials.

## Nine-stage journey

| Stage | Developer action / evidence | Friction | Plan resolution |
|---|---|---|---|
| 1. Discover | Open demo README and browser guide | Offline smoke can be mistaken for voice acceptance | Put offline, provider, browser and human evidence labels together |
| 2. Install | Use Python 3.12, prepare local Pipecat package, install browser requirements | Foundation/TTS requirements alone are not the full application | Reuse complete browser setup; retain explicit interpreter paths |
| 3. Configure | Provide `SARVAM_API_KEY` and `GOOGLE_API_KEY` to server | Credentials for distinct providers can be conflated | Presence checks name missing variable without value; validate providers independently |
| 4. First result | Run short TTS smoke with unique output directory | Default output names overwrite older runs | Unique run directory, WAV/JSON paths and scope label |
| 5. Real usage | Start browser server, allow mic/playback, speak recorded and live cases | Synthetic success is not device success | Separate physical mic, echo and audible timing acceptance |
| 6. Debug | Run STT probe and inspect boundary/timestamp evidence | Fixed fixture, no failure manifest, generic exceptions | Add input/output arguments and structured stage failures before repeated trials |
| 7. Integrate fallback | Create pinned HF checkout and sibling adapters | No existing Sarvam handler flags | Implement documented native selection only after contract inspection |
| 8. Upgrade | Test a newer pin in experiment environment | Upstream dependencies or protocol can change | Keep known-good pin and rerun adapter contract tests before changing it |
| 9. Share / return | Share sanitized report; stop experiment and restart demo | Raw transcripts and credentials could leak through broad logs | Allowlisted report, explicit local audio consent, rollback command and result |

## First-time confusion report

These timestamps are a roleplay schedule, not observations of an executed install.

- T+0:00: README leads to foundation install; note that its smoke is offline before judging provider status.
- T+0:30: Browser guide adds local package preparation; make this the single complete prerequisite reference.
- T+1:00: TTS smoke has an output-dir flag; choose a unique directory before starting the paid request.
- T+2:00: STT probe cannot accept the newly generated path; finish its CLI extension before presenting a generic input command as runnable.
- T+3:00: HF setup does not expose Sarvam selection; describe this as adapter work and show no imaginary flags.

## 1. Getting started: 6 → 8 / 10

The README already distinguishes offline configuration from live speech, and the browser guide has a complete local package preparation sequence. The plan needed an equally concrete prerequisite path for provider diagnosis: installing only `requirements-tts.txt` supplies HTTP/dotenv packages but is not the complete Pipecat dependency installation. Use the browser environment below for existing probes, and make missing dependency failures actionable before API calls. A 10 requires a clean-machine rehearsal with timed dependencies, credential availability and first generated WAV; that has not happened. Warm first-result target is 2–5 minutes, estimated, excluding account setup and dependency downloads.

### Existing commands and prerequisites

Run from the repository root. `uv` must be installed. For a new environment, reuse the existing browser guide:

```bash
uv venv demo/.venv --python 3.12
python3 demo/scripts/prepare_pipecat.py
uv pip install --python demo/.venv/bin/python 'demo/.build/pipecat[sarvam]' -r demo/requirements-browser.txt
```

For an existing working environment, do not recreate it. Put credentials in `demo/.env` or server environment, never command arguments or copied terminal output. Sarvam-only probes need `SARVAM_API_KEY`; the full interview also needs `GOOGLE_API_KEY`. The smoke loads `demo/.env` with environment precedence.

```bash
task_run_dir="demo/sarvam/results/recovery-$(date -u +%Y%m%dT%H%M%SZ)-$$"
PYTHONPATH=src demo/.venv/bin/python -m demo.scripts.smoke_sarvam_tts --language ta-IN --text 'வணக்கம். உங்கள் அனுபவத்தைச் சொல்லுங்கள்.' --output-dir "$task_run_dir" --timeout 30
```

Expected success: JSON containing audio path, bytes, lifecycle, source hash and metrics; `adapter-smoke.wav` and `adapter-smoke.json` in that run directory. This verifies generated audio, not audibility. The existing realtime probe's only switches are `--silence` and `--empty`; it requires `demo/sarvam/results/hinglish/adapter-smoke.wav` and overwrites fixed JSON output names. Do not use it as a repeatable experiment runner yet.

Existing browser launch, after both keys are available:

```bash
PYTHONPATH=src demo/.venv/bin/python -m demo.interview.server --host 127.0.0.1 --port 7860
```

### Future commands: implementation required

Extend `probe_sarvam_realtime.py` with proposed `--input-wav`, `--output-dir`, and positive `--timeout` arguments, retaining existing scenario flags. These arguments DO NOT EXIST yet. Reject unsupported WAV encoding or convert validated mono PCM through one explicit path; print result and manifest location on every terminal outcome. The post-implementation command contract is:

```text
PYTHONPATH=src demo/.venv/bin/python -m demo.scripts.probe_sarvam_realtime --input-wav <recording.wav> --output-dir <unique-run-directory> --timeout 30
```

The HF experiment requires a new README and actual runnable entry point after handler implementation. Record the inspected SHA `16d7f98ff712fb082d53497937f5456667c84680`, Python version supported by that checkout, install command, selected backend, model download expectations, launch command, port and stop command. Keep `upstream/`, `.venv/`, model cache and results within its isolated directory. Do not publish `--stt sarvam` or `--tts sarvam` examples until such flags are actually implemented and tested.

## 2. API / CLI design: 5 → 8 / 10

The TTS smoke already provides a small, discoverable argparse surface and validates output containment and timeout. Reuse that grammar for STT rather than adding a general experiment framework: input WAV, output directory, timeout, with current `--silence`/`--empty` retained. Require exit zero only for the requested stage's success; include an explicit result code in a manifest for unsupported input, provider error, timeout and interruption. First-generation HF controls should expose only implemented provider and cancellation settings. A 10 requires help output and all documented commands tested against the actual pinned adapter.

## 3. Errors and debugging: 4 → 8 / 10

Current TTS code catches RuntimeError/ValueError and formats a short terminal failure, while the STT probe can raise unhandled file, missing-key and timeout exceptions. A failure before the final write loses the durable report, so repeatable diagnosis requires a manifest written on every terminal path. Separate safe diagnostic codes from private debug details; never include authentication headers or arbitrary provider bodies. Structured errors should name stage, problem, likely cause, next action and local documentation path, with a run ID rather than a transcript dump.

| Actual code path | Current developer-visible behavior inferred from code | Proposed error contract |
|---|---|---|
| TTS constructor lacks key | `Sarvam adapter smoke failed: Sarvam TTS requires an API key` | `credential_missing`, stage TTS: “SARVAM_API_KEY is missing. Set it in demo/.env or the server environment and retry. See demo/docs/browser-interview.md.” |
| STT fixed fixture absent | `FileNotFoundError` at `wave.open(...)`, before any manifest | `input_missing`, stage input: “The requested WAV does not exist. Supply --input-wav pointing to a saved recording. See the recovery runbook.” Include safe path, never key values. |
| STT 30-second asyncio timeout | Unhandled `TimeoutError`; report not written | `provider_timeout`, stage STT: “No complete result arrived before 30 seconds. Check connectivity and the saved boundary events; retry once only if the failure is transient.” Include elapsed time and manifest path. |

These are proposals, not outputs observed from executing failures. Authentication rejection must advise fixing the key/account rather than repeatedly retrying. A 10 requires injected failure tests proving terminal text and sanitized manifests agree.

## 4. Documentation and learning: 6 → 9 / 10

The existing README has useful architecture and acceptance links, but recovery crosses several pages. Add one recovery entry link in `demo/README.md` with a four-way index: offline checks, provider checks, device checks, conditional HF comparison. The runbook must label all future commands and distinguish synthetic fixtures from human recordings, with exact working directory and expected artifacts. Keep protocol detail in adapter reference material so the first-run path stays short. A 10 requires a second contributor to complete the path without hidden setup knowledge; a playground is unnecessary for this local diagnostic task.

## 5. Upgrade and migration: 7 → 9 / 10

An isolated pinned checkout avoids changes to `demo/.venv` and the root lock, and stopping it provides a simple runtime rollback. Save the resolved dependency set and source identity with evidence so the pin is meaningful beyond a Git commit; changing a pin requires rerunning input format, auth, stream termination and cancellation tests. Do not label moving to HF an upgrade of the interview app: it would require separate acceptance of the state and control contracts already listed in the plan. No public API deprecation or codemod is needed for documentation and an isolated experiment. A 10 requires exercising rollback and proving the original demo still starts with unchanged dependencies.

## 6. Environment and tooling: 6 → 8 / 10

The existing explicit `demo/.venv/bin/python` commands prevent accidental interpreter mixing, and Python 3.12 matters because the STT probe imports `audioop`. The HF interpreter version must follow its pinned package metadata rather than assuming the demo version is compatible. Keep model/cache paths isolated, check disk space before downloading selected weights, and report download failures separately from provider failures. Reuse pytest and run_test for deterministic paths; keep live requests outside default offline CI. Python types and docstrings are the relevant editor tooling here, with no TypeScript SDK change. A 10 requires a clean Apple Silicon setup and any additionally claimed OS verified; do not claim Linux/Windows parity from this review.

## 7. Community and ecosystem: 7 → 8 / 10

Both framework sources and the application's existing test/examples provide concrete extension references, but a local Sarvam adapter is not an upstream supported integration. Keep an experiment README that names the pinned upstream license and preserves license files, documents the local adapter ownership, and distinguishes upstream failures from adapter-specific bugs. Issue reproduction should use a synthetic fixture, sanitized manifest and source pin before sharing. Provider tests can incur usage costs; document finite request counts and no unbounded automatic retry without inventing current prices. A 10 would need a maintained upstream contribution path if the adapter becomes durable; creating a new community or promising upstream acceptance is outside this diagnostic scope.

## 8. Measurement and feedback: 5 → 8 / 10

The plan already separates audible timing from synthetic event assertions and retains failed attempts. Add setup timestamps for environment start, dependencies ready, credential checks complete and first successful WAV, along with operator intervention count and failure stage. Record warm/cold runs separately and label unknown measurements rather than setting a missing value to zero. Ask a second developer to run the written sequence and report the first unclear step; use that report to improve the runbook before broader comparisons. A 10 requires measured time-to-first-result and a repeatable review after implementation, not higher scores written into a plan.

## Scorecard and acceptance

| Dimension | Initial | Amended plan | Remaining proof |
|---|---:|---:|---|
| Getting started | 6 | 8 | Clean setup timing |
| CLI/API | 5 | 8 | Real help/command contract tests |
| Errors/debugging | 4 | 8 | Injected failures and safe output |
| Documentation | 6 | 9 | Independent walkthrough |
| Upgrade/migration | 7 | 9 | Pin update and rollback rehearsal |
| Environment/tooling | 6 | 8 | Isolated install on target hardware |
| Community/ecosystem | 7 | 8 | License/repro ownership documentation |
| Measurement | 5 | 8 | Actual setup and first-result timestamps |
| Mean | 5.75 | 8.25 | Implementation unverified |

Current time to first live result is **not measured**. Warm diagnostic target: 2–5 minutes once dependencies and keys exist. Cold setup estimate: 10–30 minutes for the demo, excluding signup, network stalls or unexpected compilation; it is a planning estimate to replace with measurements. HF native Sarvam feasibility retains the CEO budget of one engineering day, not a hello-world duration. There is no credible five-minute cold HF Sarvam claim before adapters exist.

## Independent voice availability

A separate Codex CLI voice identified four issues: clean bootstrap prerequisites, executable diagnostic commands, structured terminal errors, and operational isolation. This report addresses the first three above. For isolation, the future HF launcher must use its own interpreter from its own working directory with inherited `PYTHONPATH` unset, explicitly forward only required provider environment variables, bind a documented loopback port distinct from demo port 7860, report port conflicts before initialization, and document graceful shutdown plus the existing demo restart command. Never stop unrelated processes to reclaim a port. Pin interpreter/dependency/model revisions as well as source; the implementation checklist must rehearse this operational boundary.

The existing Gemini diagnostic is `demo/scripts/probe_browser_reply.py`; reuse it rather than introducing another provider probe. Its existing command is `PYTHONPATH=src demo/.venv/bin/python -m demo.scripts.probe_browser_reply`. It loads `GOOGLE_API_KEY`, uses synthetic Hinglish input and configured Gemini model/thinking, validates completion, coaching JSON and verbatim evidence, and writes `demo/browser/results/coaching-probe.json`. It already saves many timeout/provider failures in `finally` and exits nonzero when validation fails, but missing-key failure happens before that scope and the output path is fixed. Extend it with the same proposed output-dir/timeout contract and early-failure reporting before repeated comparisons; success proves this synthetic coaching contract, not full conversational quality.

| Dimension | Claude | Independent Codex finding | Cross-model consensus |
|---|---|---|---|
| First result under five minutes | unavailable | Warm target only; cold unmeasured | N/A |
| Guessable CLI | unavailable | Align STT with existing TTS flags | N/A |
| Actionable errors | unavailable | Three traced paths need manifests/remediation | N/A |
| Findable complete docs | unavailable | Single recovery entry, runnable versus future commands | N/A |
| Safe upgrade | unavailable | Pin dependency environment and prove rollback | N/A |
| Environment friction | unavailable | Separate interpreters and caches | N/A |

## Implementation checklist and tasks

- [ ] **DX1 / P1** Extend existing realtime probe with validated input, unique output, timeout and sanitized terminal manifests; test success, missing input, missing key, provider rejection, timeout and interruption. Human: 0.5–1 day; agent: 1–3 active hours. Files: `demo/scripts/probe_sarvam_realtime.py`, focused probe tests.
- [ ] **DX2 / P1** Make TTS failure artifacts consistent with the realtime probe and document complete environment prerequisites. Human: 0.5 day; agent: 1–2 active hours. Files: `demo/scripts/smoke_sarvam_tts.py`, focused smoke tests, recovery runbook.
- [ ] **DX3 / P1** Write and rehearse the isolated HF README only with implemented commands; record source/dependency pins, ports, caches, stop/rollback and expected artifacts. Human: 0.5 day excluding adapter work; agent: 1–2 active hours. Files: proposed experiment README and dependency record.
- [ ] **DX4 / P2** Link the recovery index from demo README; measure a second developer's warm and cold walkthrough and retain confusion notes. Human: 1–2 hours plus downloads; agent: 30–60 minutes plus human walkthrough. Files: `demo/README.md`, recovery evidence/report.
- [ ] **DX5 / P1** Reuse and extend the Gemini coaching probe with unique output directories, explicit timeout and missing-key manifest; preserve its sanitized validation fields and nonzero failure behavior. Human: 2–4 hours; agent: 45–90 minutes. Files: `demo/scripts/probe_browser_reply.py`, focused probe tests.

## Decisions and scope boundaries

| ID | Decision | Class / principle | Rejected alternative |
|---|---|---|---|
| DX-A | Existing probe extension rather than new orchestration CLI | Mechanical / DRY | General experiment framework |
| DX-B | Complete browser environment as current prerequisite reference | Mechanical / explicit | TTS requirements alone as complete installation |
| DX-C | Persist safe failure manifests with terminal remediation | Mechanical / completeness | Traceback-only evidence |
| DX-D | Label future flags and unmeasured time explicitly | Mechanical / explicit | Imaginary native Sarvam flags or claimed measurements |
| DX-E | Five-minute target applies to warm provider diagnostic only | Mechanical / pragmatic | Promise cold HF integration within five minutes |
| DX-F | Rehearse pin update and rollback before reporting safe isolation | Mechanical / completeness | Assume separate folder alone proves isolation |

What already exists: README navigation, browser bootstrap, TTS argparse/output-dir conventions, environment credential loading, provider probes, safe provider field allowlist, offline test utilities and interaction acceptance docs. Reuse each at the corresponding journey step.

Not in scope: hosted playground (unrelated infrastructure); public SDK or generalized CLI (premature for a bounded experiment); guaranteed cross-platform support (requires separate device evidence); upstream Sarvam submission (consider only after a working local adapter and maintenance decision). No new cross-project TODO is required; these deferred expansions have no current implementation commitment. Required tasks stay in the recovery checklist rather than hiding core gaps as optional debt.

Durable learning: the realtime STT probe has a hard-coded 24kHz Hinglish input and success-only fixed outputs, whereas TTS already supports bounded output directories. A valid comparison first needs a repeatable probe interface; copying a new recording into the fixed filename loses useful provenance.

## Completion

DONE_WITH_CONCERNS: all eight DX passes completed; recommendations recorded as plan amendments and concrete tasks. No provider credentials inspected, provider calls made, adapter code changed or setup timing measured. Engineering review should validate the proposed error, evidence and cancellation contracts before implementation.
