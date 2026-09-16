# Current demo diagnosis

Read-only inspection, 2026-09-16. Runtime files are concurrently changing. References describe the inspected source; recheck before applying fixes. No live provider request or failure-injection test was run by this reviewer.

## Handshake failure is historical, not an established current blocker

`demo/browser/results/interaction/handshake-failure.json` records an opening timeout at 06:46:59 UTC after a valid-looking `interaction_controls_v1` advertisement. Its error text, “Browser capabilities could not be read. Continuing with voice controls.”, differs from current source. Current `server.py:221-272` validates `BrowserReady`, retries validation without malformed optional capabilities, rejects foreign session identity, then sets readiness and calls `managed.ready()`. `test_browser_server.py:259` explicitly expects malformed capabilities to preserve voice startup; the negotiated-command test exercises the valid capability path.

A subsequent `current.json` read reported success at 06:51:43; a second read reported 06:52:02, also passed/ended with no failure events. The file was overwritten during this inspection. Those are later synthetic browser/provider observations, not a stable source-linked proof or physical microphone acceptance. Do not spend a repair attempt reproducing the obsolete artifact without a new failure on captured current sources.

## 1. Output errors have no response ownership at the browser bridge

**P1, high confidence from source; reproduce with fault injection.** `_ErrorBridge.process_frame` in `pipeline.py:303` filters by TTS processor object only and invokes a zero-argument callback. `_PlaybackBridge.output_failed` at line 224 reads the session's *current* playback epoch, clears that epoch's queued outputs and calls `playback_stopped(epoch)`. Sarvam's HTTP status error (`sarvam_tts.py:130`) and caught stream error (line 183) call `push_error` without request ownership metadata or a current-request eligibility check. Normal audio publication does preserve ownership through `_emit_response_frame` in `rumik.py:562`.

An old response's delayed error can therefore be applied to a newer playback wait when the error frame is eventually processed. Even in one epoch, several queued replies are indistinguishable to this error callback. The current bridge regression test uses a `SimpleNamespace` and checks only that `[6]` is released; it does not model stale errors or two queued responses.

**Fix direction:** attach immutable playback epoch plus response/context identity to request-scoped TTS failure frames; discard obsolete errors; have the playback bridge fail only the matching owned output. Cover failed admission/queue errors separately rather than guessing the current response. Test old error A after interrupt/new B, and failure of A while B is queued in the same epoch. B's playback and idle-wait state must remain intact.

## 2. Failed speech is currently treated as completed playback

**P1 acceptance gap, verified control flow.** `session.py:935-1008` releases controller prompts/accepted answers on guard release, before successful speech delivery. That ordering preserves valid accepted evidence, but `output_failed()` then calls the normal `session.playback_stopped()` path (`session.py:341`), which clears the wait and marks the interaction listening. `_ErrorBridge` emits only a text-continuation notice. There is no explicit question-delivery-unconfirmed state in this inspected path, and partial queued audio is not invalidated by this callback.

This is intentional existing text fallback, not evidence that accepted answers should be rolled back. It does fall short of the approved recovery plan: a valid answer should remain accepted while a failed or partial next question can be replayed without another advancement.

**Fix direction:** separate output failure from successful playback completion, preserve accepted ledger/controller evidence, expose unconfirmed delivery and a retry of the same current authorized question. Invalidate residual playback on partial failure and fence old acknowledgments. Test actual session/controller state for pre-audio failure, partial playback, repeated retry and subsequent candidate speech; the current SimpleNamespace test cannot verify progression safety.

## 3. Smoke pass status can coexist with provider/control failure events

**P2, verified evidence bug.** `smoke_interaction.py:377` sets `report['passed'] = not page_errors` after reaching end. The `finally` block later gathers `failure_events` and `console_errors` but does not reconsider pass status. A run can therefore pass its selected journey while reporting an unexpected provider error or rejected command elsewhere. Current success artifacts have no such events, so this is a harness weakness, not a demonstrated false pass in those artifacts.

**Fix direction:** finalize outcome after evidence collection. Fail on unexpected provider/control errors; if a scenario intentionally exercises recovery, declare and match the expected error category and recovery outcome. Preserve unexpected browser errors separately from allowlisted diagnostics. Add a deterministic harness test for a completed journey containing an unexpected `interview.error`.

## 4. Smoke evidence can disappear or be overwritten

**P2, verified harness behavior.** WAV loading/splitting and output directory creation occur before the report's protected execution block (`smoke_interaction.py:198-218`). Browser close and Playwright stop occur before writing `current.json` (lines 406-410), so teardown failure can prevent the write. The fixed filename demonstrably changed during this inspection.

**Fix direction:** use a unique run directory, create initial run metadata before loading input, and protect final evidence writing from cleanup exceptions. Record source hashes before/after, candidate hash and configuration. Retain `current.json` only as a pointer or optional latest view. Test malformed fixture, launch failure, browser-close failure and simultaneous runs.

## Suggested root order

1. Reproduce and fix stale output failure ownership with a focused deterministic regression.
2. Implement explicit failed/partial delivery recovery while preserving accepted answers; test real session state.
3. Harden smoke outcomes and evidence, then run the current synthetic interaction smoke once against recorded sources.
4. Enter the isolated HF comparison only if a newly localized orchestration failure remains after the agreed investigation checkpoint. The historical capability timeout alone does not satisfy that condition.

No runtime files were edited. Remaining uncertainties: whether current task scheduling makes the stale-error race observable without an injected barrier; whether a newer concurrent edit already adds delivery ownership; physical audibility and device cancellation latency remain unmeasured by this review.
