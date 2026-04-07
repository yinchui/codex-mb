# Workspace New Session Option Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a "new session" option inside each workspace's second-level `/sessions` picker across Feishu, Telegram, and WeChat.

**Architecture:** Introduce a real second-level `session_picker` state that can represent both existing sessions and a synthetic `new` option tied to a workspace `cwd`. Keep `last_session_ids` for `/use` compatibility, but use `session_picker` for plain numeric replies and Telegram callback buttons.

**Tech Stack:** Python 3.9, existing channel service classes, `unittest`

---

### Task 1: Add failing tests

**Files:**
- Modify: `tests/test_codex_common.py`
- Modify: `tests/test_feishu_service.py`
- Modify: `tests/test_tg_codex_bot.py`
- Modify: `tests/test_wechat_service.py`

**Step 1: Write the failing tests**

- Add state tests for `session_picker`.
- Add channel tests proving the second-level list starts with “新建会话”.
- Add tests proving choosing that option clears active session and binds the selected workspace cwd.
- Add a Telegram callback/button test for the new option.

**Step 2: Run tests to verify they fail**

Run: `python3 -m unittest discover -s tests -v`

Expected: new assertions fail because the second-level picker only supports existing sessions today.

### Task 2: Implement shared picker state

**Files:**
- Modify: `codex_common.py`

**Step 1: Write minimal implementation**

- Add `set_session_picker(...)`
- Add `get_session_picker(...)`
- Add `clear_session_picker(...)`
- Add `is_pending_session_pick(...)` compatibility so existing callers still work

**Step 2: Run focused tests**

Run: `python3 -m unittest discover -s tests -p 'test_codex_common.py' -v`

Expected: the new state tests pass.

### Task 3: Update the three channels

**Files:**
- Modify: `feishu_longconn_service.py`
- Modify: `tg_codex_bot.py`
- Modify: `wechat_codex_service.py`

**Step 1: Write minimal implementation**

- Second-level list prepends “新建会话”.
- Numeric selection uses `session_picker` instead of only `last_session_ids`.
- Choosing `new` clears active session and sets the selected workspace cwd.
- Telegram second-level buttons also include a `new` action.
- `/use` keeps using real session ids only.

**Step 2: Run focused tests**

Run: `python3 -m unittest discover -s tests -p 'test_feishu_service.py' -v`
Run: `python3 -m unittest discover -s tests -p 'test_tg_codex_bot.py' -v`
Run: `python3 -m unittest discover -s tests -p 'test_wechat_service.py' -v`

Expected: the new session-option tests pass in all three channels.

### Task 4: Verify regression coverage

**Files:**
- Modify: none if green

**Step 1: Run full suite**

Run: `python3 -m unittest discover -s tests -v`

Expected: all tests pass.

**Step 2: Run syntax verification**

Run: `python3 -m py_compile codex_common.py feishu_longconn_service.py tg_codex_bot.py wechat_codex_service.py`

Expected: exit code 0.
