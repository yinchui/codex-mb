# Feishu Compound Send Intent Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make Feishu treat “do something, then send it to me” requests as normal Codex tasks instead of prematurely triggering the recent-attachment quick-send path.

**Architecture:** Keep the existing explicit attachment send intent flow, but add one narrow classifier for compound action-plus-send requests. The text handler should only short-circuit into quick-send when the message is a pure send instruction; otherwise it should fall through to `_run_prompt(...)` so Codex can create the artifact first.

**Tech Stack:** Python 3, unittest, existing Feishu bot service/state helpers

---

### Task 1: Add failing tests for compound action-plus-send phrases

**Files:**
- Modify: `tests/test_codex_common.py`
- Modify: `tests/test_feishu_service.py`

**Step 1: Write the failing tests**

Add tests that prove:

- `is_attachment_send_intent("把这个文件夹压缩成压缩包然后发给我")` stays explicit-send aware, but a new compound-action helper returns true.
- `service._handle_text(..., "把这个文件夹压缩成压缩包然后发给我")` does not call `send_file_path`/`send_image_path`, and instead records one Codex call.
- `service._handle_text(..., "把这个发给我")` still stays on the quick-send path.

**Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_codex_common tests.test_feishu_service -v`

Expected: new compound-send tests fail because the helper/branching logic does not exist yet.

**Step 3: Write minimal implementation**

Implement the smallest possible helper and routing change to satisfy the new tests.

**Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_codex_common tests.test_feishu_service -v`

Expected: all targeted tests pass.

**Step 5: Commit**

```bash
git add tests/test_codex_common.py tests/test_feishu_service.py codex_common.py feishu_longconn_service.py
git commit -m "fix: avoid shortcut send on compound feishu requests"
```

### Task 2: Add compound-send classifier to shared helpers

**Files:**
- Modify: `codex_common.py`
- Test: `tests/test_codex_common.py`

**Step 1: Write the failing test**

Add a focused test for the helper that returns true when a message contains both:

- an explicit send phrase
- one of the supported action words like `压缩` or `生成`

and returns false for plain send-only phrases.

**Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_codex_common.YourTestCaseName -v`

Expected: FAIL because the helper is missing.

**Step 3: Write minimal implementation**

Add a new helper in `codex_common.py`, export it, and keep the action keyword list intentionally small.

**Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_codex_common.YourTestCaseName -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add codex_common.py tests/test_codex_common.py
git commit -m "feat: classify compound feishu send requests"
```

### Task 3: Route compound requests into Codex execution

**Files:**
- Modify: `feishu_longconn_service.py`
- Test: `tests/test_feishu_service.py`

**Step 1: Write the failing test**

Add a service-level test where:

- no recent attachments exist
- input is `把这个文件夹压缩成压缩包然后发给我`

Expected behavior:

- no “当前没有可发送的最近附件” message is sent
- no direct attachment upload is attempted
- `codex.run_prompt(...)` is called once

**Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_feishu_service.YourTestCaseName -v`

Expected: FAIL because `_try_handle_attachment_send_intent(...)` currently intercepts the message.

**Step 3: Write minimal implementation**

In `_handle_text(...)`, skip the quick-send branch when the message is classified as compound action-plus-send, then let the existing `_run_prompt(...)` path handle it.

**Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_feishu_service.YourTestCaseName -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add feishu_longconn_service.py tests/test_feishu_service.py
git commit -m "fix: route compound feishu send requests to codex"
```

### Task 4: Run focused regression suite

**Files:**
- Test: `tests/test_codex_common.py`
- Test: `tests/test_feishu_service.py`

**Step 1: Run focused tests**

Run: `python3 -m unittest tests.test_codex_common tests.test_feishu_service -v`

Expected: PASS with no regressions in quick-send, picker, or permission-help behavior.

**Step 2: Run lightweight syntax verification**

Run: `python3 -m py_compile codex_common.py feishu_longconn_service.py`

Expected: PASS with no syntax errors.

**Step 3: Commit**

```bash
git add codex_common.py feishu_longconn_service.py tests/test_codex_common.py tests/test_feishu_service.py
git commit -m "test: cover compound feishu send intent routing"
```
