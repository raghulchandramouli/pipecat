# Deferred recovery work

## Interview migration, only if the isolated voice experiment qualifies

**What:** Propose a separate implementation plan for HF interview parity if matched evidence supports it.
**Why:** A successful speech round trip does not implement question progression, quote grounding, control commands, accepted-answer ownership or reconnect restoration.
**Pros:** Allows a proven voice path to support the full product. **Cons:** A second stateful application and protocol integration require sustained maintenance.
**Context:** Start from the comparison results and `demo/interview/{controller,ledger,session,reply_guard}.py`. Do not treat the HF realtime protocol as a drop-in SmallWebRTC interview transport.
**Effort:** L–XL human / M–L agent, unknown until comparison. **Priority:** P2 conditional.
**Depends on:** Qualified matched comparison, cancellation tests, explicit migration decision.

## Upstream adapter maintenance

**What:** Decide whether a working isolated Sarvam integration should be maintained locally or proposed upstream.
**Why:** An experiment becoming a permanent dependency needs an owner and an upgrade/test policy.
**Pros:** Reduces untracked divergence. **Cons:** Upstream compatibility and acceptance are not guaranteed.
**Context:** Use pinned HF contract tests and dependency records from the experiment. Preserve licenses; share synthetic reproductions only.
**Effort:** M human / S–M agent. **Priority:** P3.
**Depends on:** Successful integration and a decision to retain it beyond this experiment.

## Existing dependencies retained

Accepted-state connection restoration and broad device/audio acceptance remain P1 items in [demo/TODOS.md](../../TODOS.md). Follow-up count, adaptive timing and answer editing also remain there. This plan does not duplicate their implementations or turn unrun acceptance checks into completed work.
