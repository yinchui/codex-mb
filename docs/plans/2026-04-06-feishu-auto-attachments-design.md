# Feishu Auto Attachments Design

**Date:** 2026-04-06

**Context**

The Feishu bot currently replies with text or interactive markdown only. When Codex creates a local image or file, the bot can mention the path, but it cannot send that artifact back into the Feishu chat as a real attachment. The first phase should make the common "send that image/file to me" workflow work inside Feishu without broadening scope to Telegram or WeChat.

## Goals

- Let the Feishu bot send locally generated images and files back into the current chat as real Feishu attachments.
- Support natural follow-up messages such as "发给我", "把图片发我", and "作为附件发送".
- Limit attachment sending to files that were explicitly mentioned by the bot in recent replies.
- Keep the first phase small, predictable, and easy to test.

## Non-Goals

- No Telegram or WeChat attachment support in this phase.
- No arbitrary-path upload from user-provided paths.
- No whole-machine file search.
- No support for every file type in phase one.

## User Experience

1. Codex generates a local artifact and replies with a real local path, for example `/Users/aa/Desktop/current-screen.png`.
2. The bot records that path as a recent attachment candidate for the current Feishu conversation.
3. The user replies with an explicit send intent such as "发给我".
4. The bot resolves the most recent valid candidate and sends it as:
   - a Feishu image message for `png/jpg/jpeg/webp`
   - a Feishu file message for `pdf/md/txt/zip`
5. If the bot has multiple valid candidates, it replies with a numbered list and waits for a numeric selection.
6. If no valid candidate exists, it explains why instead of silently failing.

## Architecture

The implementation should add a small attachment pipeline beside the existing text pipeline:

- `codex_common.py` gains reusable helpers for:
  - extracting local absolute file paths from bot replies
  - classifying supported attachment types
  - detecting explicit send intent in user messages
  - persisting recent attachment candidates and pending attachment picks in `BotState`
- `FeishuAPI` gains upload/send helpers for images and generic files.
- `FeishuCodexService` gains conversation-scoped attachment state management:
  - capture candidates after each successful Codex reply
  - detect send intent before treating the user message as a normal prompt
  - resolve one candidate automatically or ask for a numeric pick when ambiguous

## Conversation State

Attachment state should be stored per Feishu conversation and actor, not globally. The storage key should combine `chat_id` and `actor_id` so that the same user in different chats does not accidentally reuse old candidates.

The stored state should include:

- `recent_attachments`: a short ordered list of recent candidates
- `pending_attachment_pick`: whether the bot is waiting for a number
- `attachment_picker`: the currently offered candidate list

Each candidate should store:

- absolute path
- detected kind: `image` or `file`
- basename
- timestamp or insertion order

Only the most recent few candidates should be retained, for example five.

## Candidate Extraction Rules

The bot should only extract candidates from its own final answer text, not from arbitrary user input.

Rules:

- Only absolute local paths are accepted.
- The file must exist at extraction time.
- The extension must be supported in this phase.
- Deduplicate identical paths while preserving recency.
- Prefer paths from the newest reply.

If a reply contains no valid path, the recent attachment state remains unchanged.

## Send Intent Rules

The bot should only start the attachment flow when the user sends a clear send-intent message. Phase-one matching can stay simple and explicit.

Examples:

- `发给我`
- `把图片发我`
- `把文件发来`
- `作为附件发送`

Messages that do not match these explicit patterns should continue through the normal prompt flow.

## Attachment Resolution

When send intent is detected:

1. Load recent candidates for the current conversation.
2. Drop any paths that no longer exist.
3. If no candidate remains, reply with a short failure message.
4. If exactly one candidate remains, upload and send it immediately.
5. If multiple candidates remain:
   - prefer a single image candidate when the user asked for "图片"
   - otherwise show a numbered picker and wait for a numeric reply

The numeric picker flow should mirror the current `/model` and `/sessions` interaction style.

## Feishu Upload Flow

`FeishuAPI` should wrap the official SDK upload endpoints:

- upload image -> receive `image_key` -> send `image` message
- upload file -> receive `file_key` -> send `file` message

This keeps transport details out of the service layer and makes tests easier.

## Error Handling

The bot should always fall back to a helpful text reply:

- no recent candidate
- candidate file missing
- unsupported type
- upload failure
- invalid numeric selection

If attachment sending fails, the original candidate list should remain available so the user can retry.

## Testing Strategy

Unit tests should cover:

- absolute-path extraction from reply text
- supported/unsupported type classification
- send-intent detection
- recent attachment state persistence
- single-candidate auto-send
- multi-candidate picker flow
- image vs file routing
- missing-file and upload-failure fallback messaging

The first phase should avoid live Feishu network dependency by using mocked API methods in `tests/test_feishu_service.py`.

## Rollout

Phase one should ship behind no extra feature flag unless testing shows a need for one. The scope is already narrow because:

- it is Feishu only
- it requires explicit send intent
- it only sends files that the bot itself referenced

This keeps the feature useful while avoiding silent or surprising uploads.
