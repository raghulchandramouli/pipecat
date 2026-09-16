# Failure recovery plan

Start with [the action plan](plan.md). Use [the decision diagram](diagrams/demo-failure-actions.svg) to choose the next diagnostic step.

The proposed fallback lives in `demo/experiments/hf-speech-to-speech/` with its own checkout, environment and adapters. That runtime has not been installed or implemented by this planning work. Sarvam keys require a Sarvam integration; they are not interchangeable with OpenAI endpoint credentials.

## Artifacts

- [Action plan](plan.md)
- [CEO review](ceo-review.md)
- [DX review](dx-review.md)
- [Engineering review](eng-review.md)
- [Test plan](test-plan.md)
- [Source snapshot and validation](evidence.json)
- [Mermaid source](diagrams/demo-failure-actions.mmd)
- [Editable Excalidraw](diagrams/demo-failure-actions.excalidraw)
- [SVG](diagrams/demo-failure-actions.svg) and [PNG](diagrams/demo-failure-actions.png)

The Excalidraw file opens with File → Open at excalidraw.com. Its boxes, arrows and text are editable.

## What is verified

The planning run inspected demo code and upstream HF revision `16d7f98ff712fb082d53497937f5456667c84680`, passed 24 focused Sarvam/ownership/repair tests, parsed all 13 interaction scenarios, and rendered and visually inspected the diagram. Live provider calls, credential validity, browser playback and physical microphone acceptance were not tested in this planning run.
