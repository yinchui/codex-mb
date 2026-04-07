# Feishu Large File Splitting Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Automatically split oversized Feishu file attachments into multiple parts and send them sequentially.

**Architecture:** Keep the existing single-file flow for normal files. When a file exceeds Feishu's upload limit, `FeishuAPI.send_file_path(...)` will create temporary chunk files, upload/send each part in order, and then send a follow-up text instruction explaining how to merge the parts locally.

**Tech Stack:** Python 3.9, `tempfile`, `pathlib`, existing `lark-oapi` client, `unittest`

---

### Task 1: Add failing multipart attachment tests

**Files:**
- Modify: `tests/test_feishu_service.py`

**Step 1: Write the failing test**

- Add a test proving an oversized file is split into multiple `.partNN` files and each part is sent.
- Add a test proving the API sends a final text message describing how to merge the parts.

**Step 2: Run test to verify it fails**

Run: `python3 -m unittest discover -s tests -p 'test_feishu_service.py' -v`

Expected: new multipart assertions fail because oversized files are currently rejected.

### Task 2: Implement multipart split/send flow

**Files:**
- Modify: `feishu_longconn_service.py`

**Step 1: Write minimal implementation**

- Add constants for upload limit and multipart chunk size.
- Add helper(s) to split one file into temporary chunk files.
- Update `send_file_path(...)` so oversized files are split and each part is sent in order.
- Add a final text message with merge instructions after all parts succeed.
- Preserve `last_attachment_error` semantics for failure paths.

**Step 2: Run targeted tests**

Run: `python3 -m unittest discover -s tests -p 'test_feishu_service.py' -v`

Expected: multipart tests pass and existing file send tests remain green.

### Task 3: Verify full regression coverage

**Files:**
- Modify: none if green

**Step 1: Run full test suite**

Run: `python3 -m unittest discover -s tests -v`

Expected: all tests pass.

**Step 2: Run syntax verification**

Run: `python3 -m py_compile codex_common.py feishu_longconn_service.py wechat_codex_service.py tg_codex_bot.py`

Expected: exit code 0.

### Task 4: Deploy to the running Feishu bot

**Files:**
- Modify: none

**Step 1: Restart Feishu service from Terminal**

- Use the existing Terminal-based `./run_feishu.sh restart` path so the process keeps macOS file permissions.

**Step 2: Verify the live process and startup log**

- Confirm a fresh `feishu_longconn_service.py` PID exists.
- Confirm the newest startup timestamp appears in `.runtime/feishu_bot.log`.
