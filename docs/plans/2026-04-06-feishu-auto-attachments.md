# Feishu Auto Attachments Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let the Feishu bot automatically send recently mentioned local images and files back into the current chat when the user explicitly asks for them.

**Architecture:** Add a small attachment pipeline beside the existing text reply flow. Shared path extraction, send-intent detection, and attachment picker state live in `codex_common.py`, while Feishu-specific upload and send operations live in `feishu_longconn_service.py`. The service captures candidates after successful replies, intercepts explicit send-intent messages, and either auto-sends a single candidate or asks for a numeric selection when multiple candidates remain.

**Tech Stack:** Python 3.9, `lark-oapi`, `unittest`, existing `BotState` JSON persistence, existing Feishu long-connection service.

---

### Task 1: Add shared attachment helpers and state

**Files:**
- Modify: `codex_common.py`
- Modify: `tests/test_codex_common.py`

**Step 1: Write the failing tests**

Add focused tests in `tests/test_codex_common.py` for:

```python
def test_extract_attachment_candidates_keeps_existing_absolute_supported_files():
    ...

def test_detect_attachment_send_intent_matches_explicit_phrases():
    ...

def test_bot_state_persists_recent_attachments_and_picker():
    ...
```

Cover:
- absolute path extraction from reply text
- supported type filtering
- explicit send-intent matching
- per-conversation attachment candidate persistence
- pending attachment picker persistence

**Step 2: Run tests to verify they fail**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_codex_common.py' -v
```

Expected:
- FAIL because the attachment helper functions and new `BotState` methods do not exist yet

**Step 3: Write minimal implementation**

In `codex_common.py`, add the smallest set of helpers needed by the tests:

```python
def extract_local_attachment_candidates(text: str) -> List[Dict[str, str]]:
    ...

def is_attachment_send_intent(text: str) -> bool:
    ...

class BotState:
    def set_recent_attachments(...): ...
    def get_recent_attachments(...): ...
    def set_attachment_picker(...): ...
    def get_attachment_picker(...): ...
    def clear_attachment_picker(...): ...
    def is_pending_attachment_pick(...): ...
```

Implementation constraints:
- only accept absolute paths
- only keep existing files
- only support `png/jpg/jpeg/webp/pdf/md/txt/zip`
- keep attachment state per conversation key

**Step 4: Run tests to verify they pass**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_codex_common.py' -v
```

Expected:
- PASS for all newly added codex-common tests

**Step 5: Commit**

```bash
git add codex_common.py tests/test_codex_common.py
git commit -m "feat: add attachment candidate helpers"
```

### Task 2: Add Feishu image/file upload helpers

**Files:**
- Modify: `feishu_longconn_service.py`
- Modify: `tests/test_feishu_service.py`

**Step 1: Write the failing tests**

Add tests in `tests/test_feishu_service.py` that exercise Feishu API behavior through a fake client or patched helper methods:

```python
def test_send_image_path_uses_feishu_image_message():
    ...

def test_send_file_path_uses_feishu_file_message():
    ...
```

Cover:
- image upload path returns an `image_key` and sends an `image` message
- file upload path returns a `file_key` and sends a `file` message
- unsupported or missing files raise a controlled failure in the service layer

**Step 2: Run tests to verify they fail**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_feishu_service.py' -v
```

Expected:
- FAIL because `FeishuAPI` has no attachment upload/send helpers yet

**Step 3: Write minimal implementation**

In `feishu_longconn_service.py`, add focused helpers to `FeishuAPI`:

```python
def send_image_path(self, chat_id: str, path: Path) -> bool:
    ...

def send_file_path(self, chat_id: str, path: Path) -> bool:
    ...
```

Implementation notes:
- use `lark.im.v1.CreateImageRequest` / `CreateFileRequest`
- use the returned `image_key` or `file_key`
- create a follow-up `CreateMessageRequest` with `msg_type("image")` or `msg_type("file")`
- keep upload/send details inside `FeishuAPI`

**Step 4: Run tests to verify they pass**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_feishu_service.py' -v
```

Expected:
- PASS for the new Feishu upload/send tests

**Step 5: Commit**

```bash
git add feishu_longconn_service.py tests/test_feishu_service.py
git commit -m "feat: add feishu attachment upload helpers"
```

### Task 3: Capture attachment candidates after successful replies

**Files:**
- Modify: `feishu_longconn_service.py`
- Modify: `tests/test_feishu_service.py`

**Step 1: Write the failing tests**

Add tests that prove successful Codex replies populate recent attachment candidates:

```python
def test_prompt_worker_records_attachment_candidates_from_answer():
    ...

def test_prompt_worker_ignores_non_attachment_answers():
    ...
```

The fake Codex answer should include a real temporary absolute path for at least one supported file.

**Step 2: Run tests to verify they fail**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_feishu_service.py' -v
```

Expected:
- FAIL because reply processing does not persist attachment candidates yet

**Step 3: Write minimal implementation**

In `feishu_longconn_service.py`:
- add a conversation-state key helper such as:

```python
def _attachment_state_key(self, chat_id: str, actor_id: str) -> str:
    return f"{chat_id}::{actor_id}"
```

- after a successful final answer is ready in `_run_prompt_worker`, extract candidates from the answer and save them:

```python
candidates = extract_local_attachment_candidates(answer)
self.state.set_recent_attachments(state_key, candidates)
```

Only update stored candidates when at least one valid candidate is found.

**Step 4: Run tests to verify they pass**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_feishu_service.py' -v
```

Expected:
- PASS for candidate-capture tests

**Step 5: Commit**

```bash
git add feishu_longconn_service.py tests/test_feishu_service.py
git commit -m "feat: capture feishu attachment candidates"
```

### Task 4: Add explicit send-intent auto-send flow

**Files:**
- Modify: `feishu_longconn_service.py`
- Modify: `tests/test_feishu_service.py`

**Step 1: Write the failing tests**

Add service-level tests for:

```python
def test_send_intent_auto_sends_single_recent_image():
    ...

def test_send_intent_auto_sends_single_recent_file():
    ...

def test_send_intent_without_candidate_returns_helpful_message():
    ...
```

Cover:
- user says `发给我`
- exactly one recent candidate exists
- the service calls `send_image_path` or `send_file_path`
- no candidate gives a fallback text reply instead of running Codex

**Step 2: Run tests to verify they fail**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_feishu_service.py' -v
```

Expected:
- FAIL because `_handle_text` still routes all non-command text into session/model flows or prompt execution

**Step 3: Write minimal implementation**

In `feishu_longconn_service.py`:
- check explicit send-intent messages near the top of `_handle_text`
- load recent candidates for the current conversation key
- if there is exactly one valid candidate:
  - route images to `self.api.send_image_path`
  - route files to `self.api.send_file_path`
- if no valid candidate exists, reply with a short failure message

Do not run Codex for these explicit send-intent messages.

**Step 4: Run tests to verify they pass**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_feishu_service.py' -v
```

Expected:
- PASS for single-candidate auto-send and missing-candidate fallback

**Step 5: Commit**

```bash
git add feishu_longconn_service.py tests/test_feishu_service.py
git commit -m "feat: auto send recent feishu attachments"
```

### Task 5: Add multi-candidate numeric picker flow

**Files:**
- Modify: `feishu_longconn_service.py`
- Modify: `tests/test_feishu_service.py`

**Step 1: Write the failing tests**

Add tests for the ambiguous case:

```python
def test_send_intent_with_multiple_candidates_prompts_for_number():
    ...

def test_numeric_attachment_pick_sends_selected_candidate():
    ...

def test_invalid_attachment_pick_returns_error_message():
    ...
```

Cover:
- multiple recent candidates exist
- service sends a numbered picker
- numeric reply sends the selected file
- invalid numbers return a helpful message
- normal messages clear stale pending attachment picks

**Step 2: Run tests to verify they fail**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_feishu_service.py' -v
```

Expected:
- FAIL because attachment pick state and numeric resolution do not exist yet

**Step 3: Write minimal implementation**

In `feishu_longconn_service.py`:
- add `_try_handle_attachment_pick(...)`
- wire it before normal chat-message execution, similar to the existing numeric session/model flows
- when multiple candidates remain:

```python
self.state.set_attachment_picker(state_key, candidates)
self.api.send_message(chat_id, numbered_picker_text)
```

- on a valid numeric reply:
  - send the chosen attachment
  - clear the attachment picker state

Also clear stale attachment-picker state when the user enters other slash commands or unrelated normal text.

**Step 4: Run tests to verify they pass**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_feishu_service.py' -v
```

Expected:
- PASS for multi-candidate picker coverage

**Step 5: Commit**

```bash
git add feishu_longconn_service.py tests/test_feishu_service.py
git commit -m "feat: add feishu attachment picker"
```

### Task 6: Full verification and manual smoke check

**Files:**
- Modify: none
- Test: `tests/test_codex_common.py`
- Test: `tests/test_feishu_service.py`

**Step 1: Run targeted tests**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_codex_common.py' -v
python3 -m unittest discover -s tests -p 'test_feishu_service.py' -v
```

Expected:
- PASS for all new shared-helper and Feishu attachment tests

**Step 2: Run broader regression suite**

Run:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile codex_common.py feishu_longconn_service.py wechat_codex_service.py tg_codex_bot.py
```

Expected:
- PASS for the full test suite
- no syntax errors

**Step 3: Manual smoke test in Feishu**

1. Ask Codex to create a temporary `.png` or `.md` file and mention its absolute path.
2. Send `发给我`.
3. Confirm the bot sends a real Feishu image or file attachment.
4. Repeat with two candidate files and confirm the numeric picker appears.

**Step 4: Commit**

```bash
git add .
git commit -m "test: verify feishu attachment sending"
```
