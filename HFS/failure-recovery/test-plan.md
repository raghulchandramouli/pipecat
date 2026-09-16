# Test plan: demo failures and isolated HF Sarvam experiment

Generated 2026-09-16 by the engineering phase of autoplan. Branch baseline: main, `6b942c0eb` plus existing uncommitted interaction changes. Record hashes of the actual source under test. This is a proposed acceptance plan; future adapters do not exist yet.

## Affected surfaces and test boundaries

Existing demo browser on its configured 7860/7861 endpoint, existing CLI probes, and proposed loopback HF experiment on 8765. No new product UI is planned. Preserve current question, accepted evidence and controls when retrying a recoverable failure; do not mistake reconnecting to a fresh session for restoration.

```text
CLI / probe [NEW extensions]
  parse arguments -> validate key + WAV + output directory
    missing/blank key -> config failure, zero requests [GAP P1]
    missing/corrupt/stereo/wrong-rate WAV -> explicit input failure [GAP P1]
    directory collision/unwritable -> no overwrite, nonzero [GAP P1]
    valid -> initial manifest -> request -> final manifest
      success / 401 / 403 / 429 / timeout / disconnect / Ctrl-C [GAP P1]
      early initialization exception / failed manifest write [GAP P1]

HF STT [NEW adapter]
  PCM16k -> bind generation + turn/revision -> send manual interval
    invalid shape/dtype/nonfinite audio -> reject before send [GAP P1]
    failed boundary/audio send -> correlation failure [GAP P1]
    partial -> caption only, no LLM [GAP P1]
    final -> current owned boundary?
      valid text -> exactly one transcription [GAP P1]
      empty/whitespace -> terminal empty, no LLM [GAP P1]
      duplicate -> idempotent; conflicting -> typed failure [GAP P1]
      old session/revision/retired boundary -> discard [GAP P1]
      no/nonfinite/reversed/ambiguous timestamps -> fail closed [GAP P1]
    deadline / queue full -> typed current-turn failure [GAP P1]
    interrupt / teardown -> close operation, clear pending [GAP P1 ->E2E]
      completion races publication -> no leak [GAP P1]
      next turn/session -> succeeds [GAP P1 ->E2E]

HF TTS [NEW adapter]
  text + response identity -> request payload/auth [GAP P1]
    blank text -> no request; bad language/model/too long -> reject [GAP P1]
    provider 401/403/429/5xx/timeout -> typed failure [GAP P1]
    200 -> validate raw PCM24k -> retain byte remainder -> stateful 16k conversion
      varied chunk splits -> identical samples, fixed HF blocks [GAP P1]
      empty/wrong type/container/odd EOF/oversize -> fail [GAP P1]
      successful EOF -> flush once, bounded zero padding [GAP P1]
      failed EOF -> no success, remaining response text suppressed [GAP P1]
    cancel before headers / during read / after first audio / at EOF
      socket closes + no stale audio/terminal event [GAP P1 ->E2E]
      response B succeeds after response A failure/cancel [GAP P1 ->E2E]

Existing demo regression surfaces [EXISTING behavior evidence]
  interval + empty + reversed final -> test_browser_stt.py [★★★]
  retired final + fresh substantive repair -> test_interaction_repair.py [★★★]
  PCM + provider failure + stalled cancellation -> test_sarvam_tts.py [★★★]
  reply marker + stale response + invalid decision -> test_reply_guard.py [★★★]
  empty final + stale/duplicate/conflicting records -> test_ledger.py [★★★]
  permission/end/retry/playback ordering -> test_browser_frontend.py [★★★]
  recovery controls/resend/old conversation -> test_interaction_frontend.py [★★★]

User trial / comparison [NEW evidence]
  clean isolated setup -> baseline -> TTS-only -> STT-only -> both [GAP ->E2E]
  real mic -> answer -> audible reply -> interrupt -> next answer [GAP ->E2E]
  rapid controls / two sessions / close tab / network loss -> no cross-talk [GAP ->E2E]
  language/quality/progression/coaching -> unchanged contract [GAP ->EVAL + human]
  manifest match?
    no -> confounded; missing/failed qualification -> NO QUALIFIED RESULT [GAP]
    yes -> all attempts + audible p95 + failure rate + ratings [GAP]
  stop experiment -> release port/devices -> original demo starts [GAP ->E2E]
```

Legend: ★★★ means inspected existing tests assert behavior, errors and edge cases; it is not a claim of full file coverage or that every listed test ran in this planning session. Every NEW adapter branch above is a gap until implemented. Upstream tests are reusable fixtures only. No line-coverage percentage is reported because future code and its final branch count do not exist.

## Required test additions and exact assertions

| ID / proposed file | Type | Input / fault | Required assertion |
|---|---|---|---|
| T1 `demo/tests/test_provider_probe_evidence.py` | unit + subprocess | missing/blank key, wrong WAV rate/width/channels, non-WAV, output collision, denied directory | no provider call; clear category/action; nonzero; existing artifacts untouched |
| T2 same file | unit + subprocess | auth failure, 429, timeout, initialization failure, interruption, output failure | one unique run ID; terminal outcome saved when writable; failed write named on stderr; secret/header/body canaries absent |
| T3 experiment `tests/test_sarvam_tts_contract.py` | unit | valid en/hi/ta requests, empty/long text, unsupported configuration | exact subscription header and Sarvam endpoint/payload; no OpenAI bearer substitution; validation before dispatch |
| T4 same file | unit | 1-second 24k PCM split at one-byte and irregular boundaries; 1kHz and 10kHz tones | same samples as single-chunk converter, anti-alias rejection, correct int16 mono blocks; meaningful duration 1s within one sample; final padding < one block |
| T5 same file | unit | empty body, JSON under 200, wrong content type, WAV/Ogg signature, odd EOF, byte cap exceeded, post-audio network error | typed response failure; no completed-success outcome; remaining response text suppressed; next response succeeds |
| T6 experiment `tests/test_sarvam_lifecycle.py` | integration with stalled loopback HTTP/WS | interrupt before headers, midstream and during completion; session end while blocked | bounded socket/task closure; no old publication after generation changes; zero leaked connection; successful next turn |
| T7 experiment `tests/test_sarvam_stt_contract.py` | unit | float or PCM input with invalid dimensions/nonfinite samples; boundary-send failure | valid audio converted once to little-endian PCM16k; invalid input rejected; failed send cannot gain owned interval |
| T8 same file | unit | partials, text final, whitespace final, duplicates, conflicts, missing/reversed/NaN timestamps | exactly one current final; blank resolves without LLM; partials cannot trigger response; conflicting/unbound finals fail closed |
| T9 `test_sarvam_lifecycle.py` | integration | A delayed, B active, reconnect; completion paused before queue publication | A never becomes B evidence; old failures/terminals cannot finish B; healthy new session succeeds |
| T10 same file | integration | maximum pending work then one extra, slow endpoint, repeated interrupt/retry | explicit overflow, accepted ordering retained, bounded buffers/workers; teardown reaches idle |
| T11 experiment `tests/test_isolation.py` | subprocess | inherited PYTHONPATH, occupied port, clean installation, teardown | correct environment/import roots and pin; no root lock or demo env modification; no kill of unrelated process; owned resources released |
| T12 experiment `tests/test_comparison_report.py` | unit | 0 samples, failure-only, missing audio, mismatched model/prompt/voice/device, cold/warm mixes | no winner for unqualified/mismatched cases; exact denominator retained; unknown is not zero latency; cold/warm separated |
| T13 existing demo focused tests | regression | actual diagnosed fault and immediate neighbors | fails before repair, passes after; no stale audio/admission/question advance; preserve current pending work |
| T14 run report | live + human | matched language/device recordings, pauses, controls, noise/long answers when claimed | audible metrics, progression opportunities and ratings meet declared gates; all failed attempts retained |

Do not create placeholder tests that assert a selector exists or a helper returns its own structure. T6/T9 must exercise a real loopback transport and publication races; output mocks alone conceal abandoned sockets. Tests are added with each implementation slice, not deferred until the entire experiment is built.

## Existing tests to reuse

`test_browser_stt.py` covers exact sent intervals, onset pre-roll, reversed final order, empty final, duplicate provider-ID conflict and invalid/ambiguous timestamps. `test_interaction_repair.py` wires actual adapter callbacks into the ledger and checks retired finals, substantive replacement and sticky timestamp failure. `test_sarvam_tts.py` covers protocol payload, PCM framing, HTTP recovery, stalled stream cancellation and invalid PCM. These are strong regression baselines for the existing demo, not tests of the proposed HF translation.

Pinned upstream `test_openai_tts_handler.py` includes `test_openai_tts_streaming_resampler_is_chunk_invariant_and_anti_aliased`, stale keyed terminals, failure after emitted audio, cancellation before headers and stalled socket release. `test_openai_stt_lifecycle.py` checks pre-publication teardown, pending-final overflow, final/progressive cancellation and new session reuse. Reuse the fixtures with the Sarvam handler so request translation and lifecycle are tested together.

## Execution sequence

1. Record source revision and dirty hashes, interpreter/dependency versions, test scope and stage configuration.
2. Rerun relevant existing deterministic tests after each diagnosed repair. A current known command from repository root is:

   ```bash
   PYTHONPATH=src demo/.venv/bin/python -m pytest -c demo/pytest.ini demo/tests/test_sarvam_tts.py demo/tests/test_browser_stt.py demo/tests/test_interaction_repair.py -q
   ```

3. Add T1–T2 while hardening probes, then perform the existing raw provider probes with synthetic input. Validate Gemini separately using `demo/scripts/probe_browser_reply.py`.
4. Prepare the isolated environment from the DX runbook, run upstream supported-backend baseline, then implement T3–T11 alongside the handlers. Future commands belong in the experiment README only after the referenced files and launch integration exist.
5. Run combined audio, cancellation and next-turn qualification before expanded trials. Include setup warmups in cost accounting but exclude them from response latency distributions.
6. For changes in prompt, controller, reply guard, STT ownership or endpointing, execute applicable `demo/evals/interaction/` scenarios and browser smoke/interruption flows. Compare real results, not YAML parsing, with the preserved baseline.
7. Run T12–T14 and rollback checks. A failed qualification keeps the experiment unaccepted; do not hide it by retrying until one pass remains.

## Human and device acceptance

First matrix: English, Tanglish and Hinglish crossed with quiet/headset and quiet/laptop speakers. Collect at least 30 logical response turns per language/device condition across at least three sessions, and independently exercise every progression/control/repair opportunity at least once per session. Record all attempted turns, failed requests, actual last user speech and first audible bot sample, speech-start and final audible bot sample during interruption. Use one recording clock or an explicitly synchronized capture arrangement.

Targets: p95 answer-end to audible reply <=5s; speech-start to audible stop <=500ms; zero accidental question advances across the declared opportunities; naturalness/control >=4/5 from the owner plus two speakers per offered language. These thresholds are proposed acceptance gates, not results. Mark a configuration unqualified if playback cannot be measured. Add noise, long answers and restoration tests before broad supported-environment claims; accepted-state restoration remains its own existing TODO.

Critical user flows: first answer with microphone permission, speaker retry without losing progress, interrupt and answer again, empty/noisy speech without advancement, late final during repair, rapid repeat/skip controls, end while connecting, tab closure, network loss, and fresh session after a failed provider request. Validate a visible actionable recovery state and absence of stale audio for every failure.

## Evidence status

Parent planning run: 24 selected existing tests passed; 13 interaction scenario files parsed. Provider keys, live STT/TTS/Gemini calls, HF Sarvam integration, physical microphones, audible p95 and human ratings are NOT VERIFIED by this review. Keep future run outcomes separated into unit, loopback integration, provider-backed synthetic, physical device and human evidence.

## Final engineering additions

- **T15, proposed `demo/tests/test_question_delivery_recovery.py` plus browser integration:** inject TTS failure before audio, partial delivery, and failure after client buffering. Preserve the accepted answer, mark question delivery unconfirmed, retry the same authorized question ID, and assert no second progression transition. Multiple retry clicks are idempotent; stale playback acknowledgment cannot confirm the retry. This tests the proposed delivery policy, not existing behavior.
- Extend **T6** to cancel after audio has entered the browser buffer. Assert the actual device becomes silent within the target, buffered old audio never resumes, teardown finishes, and the next authorized reply is audible. Measure silence rather than trusting a cancel event.
- Extend **T4** to reset filter/carry/tail between responses and after cancellation. A response following cancelled audio must equal its standalone conversion, and no padding or filter tail from the old response may leak.
- Extend **T12/T14** with replies that never start, partial replies and unsynchronized clocks. Never-started replies are failures with missing latency, not zero-duration samples or dropped rows. Only synchronized capture yields an audible latency result; all other timing is labeled a stage metric.
- If the demo adapter itself is implicated, use an independently constructed minimal Sarvam HTTP request as the TTS provider control. Existing `smoke_sarvam_tts.py` traverses demo admission/adapter code and therefore cannot alone distinguish provider failure from that code.
- Isolation tests must verify `UV_PROJECT_ENVIRONMENT` selects the experiment `.venv`, record the lock generated by the first unfrozen upstream sync, and classify the different-model Mac baseline as installation evidence only.

Concurrent `/demo` edits mean the existing passing run is point-in-time evidence. The captured source manifest is useful, but tree stability across test execution was not enforced. Before accepting a repair or comparison, record before/after relevant-source hashes, rerun if they differ, and attach those hashes to the result.
