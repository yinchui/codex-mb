# Two-Level Session Picker Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Change `/sessions` into a two-level picker that selects workspace first and session second across Feishu, Telegram, and WeChat.

**Architecture:** Extend `BotState` with workspace-picker state, add shared helper logic for grouping recent sessions by `cwd`, then update each channel service so `/sessions` shows workspaces first and numeric replies advance into a second session list before switching.

**Tech Stack:** Python 3.9, existing service classes, `unittest`

---

### Task 1: Add failing state and picker tests

**Files:**
- Modify: `tests/test_feishu_service.py`
- Modify: `tests/test_tg_codex_bot.py`
- Modify: `tests/test_wechat_service.py`
- Modify: `tests/test_codex_common.py`

**Step 1: Write the failing tests**

- Add tests proving `/sessions` first returns grouped workspaces.
- Add tests proving a workspace number triggers a second-level session list.
- Add tests proving a second-level number switches to the session.
- Add state tests for workspace picker persistence/clearing.

**Step 2: Run tests to verify they fail**

Run: `python3 -m unittest discover -s tests -v`

Expected: new assertions fail because only one-level session picking exists today.

### Task 2: Implement shared two-level picker state

**Files:**
- Modify: `codex_common.py`

**Step 1: Write minimal implementation**

- Add workspace picker state getters/setters/clearers.
- Add any small shared grouping helper needed by all channels.
- Keep existing session picker support for second-level selections.

**Step 2: Run focused tests**

Run: `python3 -m unittest discover -s tests -p 'test_codex_common.py' -v`

Expected: state tests pass.

### Task 3: Update channel handlers

**Files:**
- Modify: `feishu_longconn_service.py`
- Modify: `tg_codex_bot.py`
- Modify: `wechat_codex_service.py`

**Step 1: Write minimal implementation**

- Update `/sessions` to show workspaces first.
- Add numeric handling for workspace selection before session selection.
- Keep `/use <session_id>` and `/history` compatibility.
- Clear workspace/session picker state when other numeric-pick flows take over.

**Step 2: Run focused tests**

Run: `python3 -m unittest discover -s tests -p 'test_feishu_service.py' -v`
Run: `python3 -m unittest discover -s tests -p 'test_tg_codex_bot.py' -v`
Run: `python3 -m unittest discover -s tests -p 'test_wechat_service.py' -v`

Expected: all new picker tests pass.

### Task 4: Verify full regression coverage

**Files:**
- Modify: none if green

**Step 1: Run full suite**

Run: `python3 -m unittest discover -s tests -v`

Expected: all tests pass.

**Step 2: Run syntax verification**

Run: `python3 -m py_compile codex_common.py feishu_longconn_service.py wechat_codex_service.py tg_codex_bot.py`

Expected: exit code 0.
