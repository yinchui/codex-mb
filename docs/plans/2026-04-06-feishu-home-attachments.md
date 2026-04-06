# Feishu Home Directory Attachments Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let the Feishu bot send most files under the current user's home directory on explicit upload intent, while refusing high-risk secrets and surfacing macOS permission failures clearly.

**Architecture:** Extend the shared attachment helpers with home-directory allow/deny policy checks and explicit file-name hint parsing, then update the Feishu service to filter candidates through that policy before upload. Preserve the existing picker flow, keep prompt guidance steering newly created artifacts into the runtime attachment directory, and add startup/runtime permission diagnostics for TCC-protected folders.

**Tech Stack:** Python 3.9, `unittest`, existing `BotState` JSON persistence, existing Feishu long-connection service, macOS file permission behavior.

---

### Task 1: Add shared home-directory attachment policy helpers

**Files:**
- Modify: `codex_common.py`
- Modify: `tests/test_codex_common.py`

**Step 1: Write the failing tests**

Add focused tests in `tests/test_codex_common.py` for:

```python
def test_is_allowed_home_attachment_path_accepts_supported_file_under_home():
    ...

def test_is_allowed_home_attachment_path_rejects_sensitive_secret_paths():
    ...

def test_extract_attachment_name_hints_finds_explicit_file_names():
    ...
```

Cover:
- supported absolute file under `Path.home()`
- unsupported extension rejection
- path outside `Path.home()` rejection
- denylist matches for `.ssh`, `.gnupg`, `.aws`, `.env`, `id_rsa`, `id_ed25519`, `keychain`, `secret`, `token`, `credential`
- extracting explicit file-name hints such as `Kimi_Attention_Residuals_2603.15031.pdf，把这个发给我`

**Step 2: Run tests to verify they fail**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_codex_common.py' -v
```

Expected:
- FAIL because the new policy helpers and file-name hint parser do not exist yet

**Step 3: Write minimal implementation**

In `codex_common.py`, add the smallest reusable helpers needed by the tests:

```python
def is_allowed_home_attachment_path(path: Path) -> Tuple[bool, Optional[str]]:
    ...

def extract_attachment_name_hints(text: str) -> List[str]:
    ...
```

Implementation notes:
- normalize with `expanduser()` and `resolve()` where safe
- require the path to stay under `Path.home()`
- keep the denylist explicit and small
- return a machine-usable reason for rejection such as `outside_home`, `sensitive_path`, `unsupported_type`
- keep the hint extractor simple: basename-like tokens with supported extensions only

**Step 4: Run tests to verify they pass**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_codex_common.py' -v
```

Expected:
- PASS for the new home-attachment policy tests

**Step 5: Commit**

```bash
git add codex_common.py tests/test_codex_common.py
git commit -m "feat: add home attachment policy helpers"
```

### Task 2: Apply home-directory policy and file-name hints in Feishu candidate resolution

**Files:**
- Modify: `feishu_longconn_service.py`
- Modify: `tests/test_feishu_service.py`

**Step 1: Write the failing tests**

Add service-level tests in `tests/test_feishu_service.py` for:

```python
def test_send_intent_allows_recent_home_file_candidate():
    ...

def test_send_intent_rejects_sensitive_candidate_with_clear_message():
    ...

def test_send_intent_prefers_candidate_matching_explicit_file_name_hint():
    ...
```

Cover:
- direct upload from a readable file under the temp home fixture
- sensitive-path rejection without calling the Feishu upload API
- matching explicit file-name hints against recent candidates before picker fallback

**Step 2: Run tests to verify they fail**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_feishu_service.py' -v
```

Expected:
- FAIL because Feishu resolution currently does not use the shared home-policy helpers or file-name hints

**Step 3: Write minimal implementation**

In `feishu_longconn_service.py`:
- update recent-candidate filtering to call `is_allowed_home_attachment_path`
- if the upload-intent text includes a specific basename hint, prefer candidates whose `name` matches exactly
- preserve the existing single-candidate auto-send and multi-candidate picker behavior
- reply with a clear Chinese reason when the candidate is rejected by policy

Suggested structure:

```python
def _recent_attachment_candidates(self, chat_id: str, actor_id: str) -> List[Dict[str, str]]:
    ...

def _match_attachment_hint(self, text: str, candidates: List[Dict[str, str]]) -> List[Dict[str, str]]:
    ...
```

**Step 4: Run tests to verify they pass**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_feishu_service.py' -v
```

Expected:
- PASS for the new allow/reject/hint-resolution tests

**Step 5: Commit**

```bash
git add feishu_longconn_service.py tests/test_feishu_service.py
git commit -m "feat: allow home directory attachment candidates"
```

### Task 3: Add startup permission probes and permission-aware send failures

**Files:**
- Modify: `feishu_longconn_service.py`
- Modify: `tests/test_feishu_service.py`

**Step 1: Write the failing tests**

Add tests for:

```python
def test_send_intent_permission_error_returns_guidance_message():
    ...

def test_permission_probe_reports_tcc_blocked_directories():
    ...
```

Cover:
- `PermissionError` raised from `send_image_path` or `send_file_path` no longer escapes the event handler
- the reply includes guidance that this is a system permission problem
- a startup/helper probe logs or returns blocked status for `~/Desktop`, `~/Documents`, `~/Downloads`

**Step 2: Run tests to verify they fail**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_feishu_service.py' -v
```

Expected:
- FAIL because the permission probe helper does not exist yet and the message text is not finalized

**Step 3: Write minimal implementation**

In `feishu_longconn_service.py`:
- add a small permission probe helper, for example:

```python
def _probe_home_directory_permissions(self) -> Dict[str, str]:
    ...
```

- call it during startup and log blocked/common readable folders
- keep catching `PermissionError` and `OSError` in `_send_attachment_candidate`
- standardize the Chinese guidance message so the user knows the Feishu service lacks system file access

**Step 4: Run tests to verify they pass**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_feishu_service.py' -v
```

Expected:
- PASS for permission probe and permission-guidance tests

**Step 5: Commit**

```bash
git add feishu_longconn_service.py tests/test_feishu_service.py
git commit -m "feat: report feishu file permission status"
```

### Task 4: Refine prompt guidance for newly created artifacts

**Files:**
- Modify: `feishu_longconn_service.py`
- Modify: `tests/test_feishu_service.py`

**Step 1: Write the failing test**

Add a focused test for:

```python
def test_prompt_worker_includes_runtime_attachment_dir_guidance():
    ...
```

Cover:
- the effective Codex prompt includes the Feishu runtime attachment directory
- the guidance tells Codex not to save newly created artifacts to `~/Desktop`, `~/Documents`, or `~/Downloads`

**Step 2: Run test to verify it fails**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_feishu_service.py' -v
```

Expected:
- FAIL if the current prompt guidance does not exactly match the new desired wording

**Step 3: Write minimal implementation**

Keep the prompt injection small and local:

```python
def _attachment_output_dir(self) -> Path:
    ...

def _prompt_with_attachment_guidance(self, prompt: str) -> str:
    ...
```

Implementation notes:
- preserve the current runtime attachment directory under the Feishu state root
- only add guidance text; do not change unrelated prompt execution logic

**Step 4: Run test to verify it passes**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_feishu_service.py' -v
```

Expected:
- PASS for prompt guidance coverage

**Step 5: Commit**

```bash
git add feishu_longconn_service.py tests/test_feishu_service.py
git commit -m "feat: steer feishu artifacts into runtime attachment dir"
```

### Task 5: Full verification and manual macOS permission check

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
- PASS for the new shared-helper and Feishu service tests

**Step 2: Run broader regression**

Run:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile codex_common.py feishu_longconn_service.py wechat_codex_service.py tg_codex_bot.py
```

Expected:
- PASS for the full unit suite
- no syntax errors

**Step 3: Manual smoke check**

1. Restart the LaunchAgent-backed Feishu service.
2. Ask the bot to create a new file under the runtime attachment directory and verify `发给我` uploads it.
3. Ask the bot about a readable home-directory file, for example a PDF on Desktop, and verify explicit upload intent works when macOS permissions allow it.
4. Try a blocked path such as `.env` or a mock `.ssh` key fixture and confirm the bot refuses it.
5. If macOS denies Desktop access, confirm the chat reply clearly explains the system permission issue.

**Step 4: Commit**

```bash
git add .
git commit -m "test: verify feishu home attachment access"
```
