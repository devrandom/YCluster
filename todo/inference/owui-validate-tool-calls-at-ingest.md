# OWUI: validate tool-call arguments at ingest, not on replay

Open WebUI persisted a malformed assistant tool call (`function.arguments`
contained valid JSON up to a point, then trailing garbage — table markup
fragments and a stray `\r\n`) into `chat.history.messages` without validating
it. The corruption originated in SGLang's `glm47` tool-call parser
(image `glm53-flash-nvfp4-sglang:sm120-20260829`, built 2026-09-07), but OWUI
is the layer that turned a single bad completion into a permanently stuck chat.

What happens today:

1. Backend streams a tool call with invalid JSON arguments.
2. OWUI persists it as-is into chat history.
3. The *next* request replays the broken history; `process_chat` validates the
   outgoing payload and 400s with `Assistant tool call function.arguments must
   be valid JSON.` (local-ai-proxy log, 4× on 2026-09-08 for `glm-5.3-flash`).
4. Each failed retry adds an empty assistant error placeholder to the chat,
   and the loop can never recover — regenerate re-sends the same poisoned
   history.

The validation OWUI needs already exists (`process_chat`); it just runs on the
wrong side. Fix: validate (or sanitize) `tool_calls[].function.arguments` when
the assistant response arrives, before persisting. On failure, surface the
error on the failed message only and keep the history clean so a regenerate
works.

## Acceptance

- A completion with malformed `function.arguments` is not persisted as a
  tool call; the error is attached to that message only.
- Regenerating from the turn *before* the malformed call produces a working
  reply without manual DB surgery.
- Existing chats that already contain a malformed call (e.g. the
  `update_memory` one from 2026-09-08, "Dual Tendinopathy Flare Plan") can be
  repaired from the UI — deleting the broken assistant message should be
  sufficient to unblock the chat.

## Related

- Upstream SGLang `glm47` parser bug (trailing bytes spliced into arguments
  blob; possibly triggered by table markup / `\r\n` in streamed output) — file
  a sanitized report (corruption signature only, no chat content).
- local-ai-proxy could optionally validate arguments inbound, but passthrough
  is defensible; OWUI is the right enforcement point since it owns
  persistence.
