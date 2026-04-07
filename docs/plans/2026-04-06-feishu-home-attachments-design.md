# Feishu Home Directory Attachments Design

**Date:** 2026-04-06

**Context**

The current Feishu attachment flow can upload files only when the bot process can read the referenced local path. In practice, the LaunchAgent-backed Feishu service can read its own runtime attachment directory, but macOS privacy controls can still block paths under the user's home directory such as `~/Desktop`, `~/Documents`, and `~/Downloads`. The next phase should expand the feature from "bot-generated safe-directory artifacts" to "most user home-directory files", while keeping strong intent gating and refusing obviously sensitive secrets.

## Goals

- Let the Feishu bot send most files under the current user's home directory when the user explicitly asks for upload.
- Preserve the natural follow-up workflow: messages such as `发给我`, `发送`, `上传`, or `xxx.pdf，把这个发给我`.
- Detect and report permission failures clearly instead of letting the message handler fail silently.
- Keep a conservative denylist for highly sensitive files and directories.
- Preserve the existing numeric picker flow when multiple valid candidates exist.

## Non-Goals

- No arbitrary full-disk read outside the current user's home directory in this phase.
- No background indexing or whole-machine scanning.
- No automatic upload without explicit user intent.
- No removal of all safety checks; high-risk secrets remain blocked by policy.
- No Telegram or WeChat scope changes.

## User Experience

1. The bot mentions a real absolute path inside the user's home directory, or the user explicitly names a file in follow-up chat.
2. The user sends an explicit upload intent such as `发给我` or `Kimi_Attention_Residuals_2603.15031.pdf，把这个发给我`.
3. The bot validates that the candidate path:
   - lives under `Path.home()`
   - is not blocked by the denylist
   - exists and is a supported file type
4. If the file is readable, the bot uploads it as a Feishu image or file attachment.
5. If the file is blocked by macOS privacy permissions, the bot replies with a clear explanation and a next step.
6. If the file is blocked by security policy, the bot says that it is intentionally disallowed.
7. If more than one candidate remains, the bot reuses the existing numbered picker flow.

## Recommended Approach

Keep the current LaunchAgent-driven Feishu bot architecture and extend the existing attachment pipeline. The bot should allow attachments from the user's home directory, but only when the user sends explicit upload intent and the path survives a policy check.

This approach keeps the current deployment model, minimizes engineering churn, and addresses the real failure mode we observed: the bot was already resolving the right file, but the service could not read a TCC-protected Desktop path. The design therefore adds two layers:

- a broader attachment policy that allows most home-directory files
- a clearer permission/error path that either uploads directly or guides the user when macOS denies access

## Permission Model

The effective allowed root for phase two is the current user's home directory:

- `Path.home()`

The bot should no longer require files to live only in the Feishu runtime `attachments` directory. Instead, it should treat the runtime directory as a fallback safe cache and staging area, while allowing direct upload from home-directory paths when permitted.

The service should perform a startup self-check and log whether common user folders appear readable:

- `~/Desktop`
- `~/Documents`
- `~/Downloads`

If these probes raise `PermissionError` or `Operation not permitted`, the bot should treat that as a system-permission issue and report it in a user-friendly way during upload attempts.

## Attachment Policy

### Allow rules

- The candidate path must be absolute.
- The candidate path must resolve under `Path.home()`.
- The file must exist and be a regular file.
- The extension must remain within the supported upload set for this phase:
  - images: `png`, `jpg`, `jpeg`, `webp`
  - files: `pdf`, `md`, `txt`, `zip`

### Deny rules

Even inside the home directory, the bot should refuse known high-risk paths and names.

Default denylist:

- directories:
  - `~/.ssh`
  - `~/.gnupg`
  - `~/.aws`
- path fragments or file names containing:
  - `.env`
  - `id_rsa`
  - `id_ed25519`
  - `keychain`
  - `private_key`
  - `secret`
  - `token`
  - `credential`

The denylist is intended to block obviously dangerous secret material, not to provide a perfect enterprise DLP policy. It should remain small, explicit, and easy to explain.

## Candidate Resolution

The current feature already stores recent attachment candidates per `chat_id::actor_id`. That model should remain in place.

Candidate resolution for upload intent should become:

1. Load recent candidates for the current conversation.
2. Keep only those that:
   - still exist
   - live under `Path.home()`
   - pass the denylist
3. If the user explicitly names a file in the send-intent message, prefer matching recent candidates by basename first.
4. If exactly one candidate remains, upload it.
5. If multiple candidates remain, present the numbered picker.
6. If none remain, reply with a helpful explanation.

The bot should not scan the whole home directory on every message. In this phase, it should continue to rely primarily on files that were already mentioned in the current conversation, plus explicit file-name hints in the send-intent message.

## macOS Permission Failures

The observed failure mode is a thrown `PermissionError` while trying to open Desktop files from the background Feishu service. This should no longer surface as a generic handler failure.

Required behavior:

- Catch `PermissionError` and related `OSError` cases in the attachment send path.
- Reply with a clear Chinese message stating that the Feishu service currently lacks system permission to read that folder.
- Keep the candidate available for retry after permissions are fixed.
- Log the denied path and error type for debugging.

If the product later proves that LaunchAgent-based access is fundamentally too unreliable, we can revisit a frontmost helper or bridge process. That is not needed in this phase.

## Prompt Guidance for Newly Created Files

When the bot asks Codex to generate new local artifacts, it should continue nudging Codex to use the Feishu runtime `attachments` directory rather than Desktop. This reduces reliance on TCC-protected folders for newly created screenshots and exports.

That guidance should remain best-effort, not an exclusive requirement. If Codex still references a home-directory file elsewhere, the upload path should validate and attempt to use it.

## Error Handling

All attachment failures must produce explicit user-facing replies:

- no candidate available
- unsupported file type
- blocked sensitive file
- outside-home-directory path
- macOS permission denied
- Feishu upload failure
- invalid numeric picker selection

The bot should never silently drop the message or let the event handler crash on these cases.

## Testing Strategy

Unit coverage should include:

- allow home-directory paths that are supported and readable
- reject sensitive denylist paths
- preserve existing picker behavior
- match explicit file-name hints in upload-intent messages
- surface `PermissionError` as a helpful Chinese reply
- include prompt guidance for newly created files to use the runtime attachment directory

Regression coverage should keep existing `/model`, `/account`, sessions, Telegram, and WeChat tests green.

## Rollout and Reversal

Rollout should remain unflagged but conservative:

- explicit user intent is still required
- high-risk files remain blocked
- only home-directory paths are considered in this phase

If the broader home-directory policy proves too risky, rollback is simple:

- revert the allow rule from `Path.home()` back to the Feishu runtime attachment directory only
- keep the improved permission error messaging and picker flow
