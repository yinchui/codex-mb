# Attachment Scope Expansion Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Allow Feishu attachments for `docx`, `xlsx`, and `mp4`, and allow safe attachments from `Home` plus mounted volumes under `/Volumes/*`.

**Architecture:** Keep the existing recent-attachment pipeline and expand only the attachment policy gate in `codex_common.py`. Preserve current sensitive-path blocking and managed attachment behavior, while widening the accepted file extensions and allowed roots.

**Tech Stack:** Python 3, unittest, Feishu long-connection service

---

### Task 1: Expand policy tests

**Files:**
- Modify: `tests/test_codex_common.py`
- Modify: `tests/test_feishu_service.py`

**Step 1: Write the failing tests**

- Add a codex helper test that accepts `docx`, `xlsx`, and `mp4`.
- Add a codex helper test that accepts `/Volumes/<disk>/...` paths.
- Add a Feishu service test that sends an allowed `/Volumes/...` attachment.

**Step 2: Run targeted tests to verify they fail**

Run: `python3 -m unittest tests.test_codex_common tests.test_feishu_service -v`

Expected: new attachment policy tests fail because unsupported extensions and `/Volumes` paths are still rejected.

### Task 2: Expand attachment policy

**Files:**
- Modify: `codex_common.py`
- Modify: `feishu_longconn_service.py`

**Step 1: Write minimal implementation**

- Extend `SUPPORTED_ATTACHMENT_EXTENSIONS` and filename hint matching for `docx`, `xlsx`, `mp4`.
- Expand the attachment policy helper so it accepts safe files inside `Home` or `/Volumes/<mount>/...`.
- Keep sensitive directory and token blocking intact.
- Update the rejection text so it reflects the broader allowed scope.

**Step 2: Run targeted tests to verify they pass**

Run: `python3 -m unittest tests.test_codex_common tests.test_feishu_service -v`

Expected: targeted tests pass.

### Task 3: Full verification and service reload

**Files:**
- None

**Step 1: Run full verification**

Run: `python3 -m py_compile codex_common.py feishu_longconn_service.py tg_codex_bot.py wechat_codex_service.py feishu_launchd_bootstrap.py`

Run: `python3 -m unittest discover -s tests -v`

Expected: all checks pass.

**Step 2: Restart Feishu service**

Run: `launchctl bootout ... && launchctl bootstrap ...`

Expected: service restarts cleanly and logs show the startup probe remains readable.
