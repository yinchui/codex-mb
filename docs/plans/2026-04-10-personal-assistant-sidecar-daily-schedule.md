# Personal Assistant Sidecar Daily Schedule Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 保留现有飞书多工作区对话与私人助手主动提醒能力的前提下，新增“跨工作区轻量旁路同步”和“前一晚 22:00 协商式次日日程建议稿 + 次日按时间点跟进 + 回写大任务”的完整链路。

**Architecture:** 继续让飞书当前工作区负责主回复，同时在后台追加一次只写私人助手状态的 sidecar prompt，把任务类事实同步进私人助手工作区。每日计划部分单独维护 `daily_schedule.json` 和两条新自动化：一条在每天 22:00 生成次日日程建议稿，另一条按小时检查已确认的时间点并温和询问进度；用户的确认、修改、进度回复仍通过 `personal-assistant-chat` 统一回写到大任务。

**Tech Stack:** Python (`feishu_longconn_service.py`, `codex_common.py`), unittest/pytest, 私人助手工作区中的 Markdown skill + YAML/JSON 状态文件, Codex automation TOML

---

### Task 1: 为 sidecar 同步建立可测试的配置与会话基元

**Files:**
- Create: `/Volumes/YC/AI产品/codex-mb/personal_assistant_bridge.py`
- Modify: `/Volumes/YC/AI产品/codex-mb/codex_common.py`
- Create: `/Volumes/YC/AI产品/codex-mb/tests/test_personal_assistant_bridge.py`
- Modify: `/Volumes/YC/AI产品/codex-mb/tests/test_codex_common.py`

**Step 1: Write the failing test**

在 `/Volumes/YC/AI产品/codex-mb/tests/test_personal_assistant_bridge.py` 新增最小失败用例，覆盖“从环境变量读取私人助手配置”和“构造 sidecar prompt”；在 `/Volumes/YC/AI产品/codex-mb/tests/test_codex_common.py` 新增 `BotState` 的辅助会话用例，确保不会覆盖主工作区会话。

```python
from pathlib import Path

from codex_common import BotState
from personal_assistant_bridge import load_personal_assistant_config, build_sidecar_sync_prompt


def test_load_personal_assistant_config_reads_assistant_workspace() -> None:
    cfg = load_personal_assistant_config(
        {
            "PERSONAL_ASSISTANT_CWD": "/tmp/assistant",
            "PERSONAL_ASSISTANT_SIDE_SYNC_ENABLED": "1",
        }
    )
    assert cfg is not None
    assert cfg.cwd == Path("/tmp/assistant")
    assert cfg.side_sync_enabled is True


def test_build_sidecar_sync_prompt_mentions_source_workspace() -> None:
    prompt = build_sidecar_sync_prompt(
        user_text="今天先把专利提纲改完",
        source_cwd=Path("/tmp/project-a"),
        skill_path=Path("/tmp/assistant/skills/personal-assistant-chat/SKILL.md"),
        state_dir=Path("/tmp/assistant/state"),
    )
    assert "project-a" in prompt
    assert "personal-assistant-chat" in prompt
    assert "不要向用户发送第二条飞书消息" in prompt


def test_bot_state_tracks_aux_session_without_overwriting_active_session(tmp_path: Path) -> None:
    state = BotState(tmp_path / "feishu_bot_state.json")
    state.set_active_session("ou-owner", "main-1", "/tmp/main")
    state.set_aux_session("ou-owner", "personal_assistant", "pa-1", "/tmp/assistant")

    assert state.get_active("ou-owner") == ("main-1", "/tmp/main")
    assert state.get_aux_session("ou-owner", "personal_assistant") == ("pa-1", "/tmp/assistant")
```

**Step 2: Run test to verify it fails**

Run:
```bash
python3 -m pytest \
  /Volumes/YC/AI产品/codex-mb/tests/test_personal_assistant_bridge.py \
  /Volumes/YC/AI产品/codex-mb/tests/test_codex_common.py -v
```

Expected: FAIL，报错类似 `ModuleNotFoundError: No module named 'personal_assistant_bridge'` 或 `AttributeError: 'BotState' object has no attribute 'set_aux_session'`。

**Step 3: Write minimal implementation**

在 `/Volumes/YC/AI产品/codex-mb/personal_assistant_bridge.py` 中加入一个轻量 dataclass 和 prompt builder；在 `/Volumes/YC/AI产品/codex-mb/codex_common.py` 的 `BotState` 中加入命名空间化的辅助会话存取方法，避免 sidecar 会话覆盖当前工作区会话。

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional


@dataclass(frozen=True)
class PersonalAssistantConfig:
    cwd: Path
    side_sync_enabled: bool


def load_personal_assistant_config(env: Mapping[str, str]) -> Optional[PersonalAssistantConfig]:
    raw_cwd = str(env.get("PERSONAL_ASSISTANT_CWD") or "").strip()
    if not raw_cwd:
        return None
    enabled = str(env.get("PERSONAL_ASSISTANT_SIDE_SYNC_ENABLED") or "1").strip() not in {"0", "false", "False"}
    return PersonalAssistantConfig(cwd=Path(raw_cwd).expanduser(), side_sync_enabled=enabled)


def build_sidecar_sync_prompt(user_text: str, source_cwd: Path, skill_path: Path, state_dir: Path) -> str:
    return (
        f"使用[$personal-assistant-chat]({skill_path}) 进行轻量任务提取。"
        f"消息来源工作区: {source_cwd}\n"
        f"只更新 {state_dir} 下的状态文件，不要向用户发送第二条飞书消息。\n"
        f"原始飞书消息: {user_text}"
    )
```

**Step 4: Run test to verify it passes**

Run:
```bash
python3 -m pytest \
  /Volumes/YC/AI产品/codex-mb/tests/test_personal_assistant_bridge.py \
  /Volumes/YC/AI产品/codex-mb/tests/test_codex_common.py -v
```

Expected: PASS，新增用例全部通过，且原有 `BotState` 相关用例不回归。

**Step 5: Commit**

```bash
git -C /Volumes/YC/AI产品/codex-mb add \
  /Volumes/YC/AI产品/codex-mb/personal_assistant_bridge.py \
  /Volumes/YC/AI产品/codex-mb/codex_common.py \
  /Volumes/YC/AI产品/codex-mb/tests/test_personal_assistant_bridge.py \
  /Volumes/YC/AI产品/codex-mb/tests/test_codex_common.py

git -C /Volumes/YC/AI产品/codex-mb commit -m "feat: add personal assistant sidecar primitives"
```

### Task 2: 让飞书主回复后静默旁路同步私人助手状态

**Files:**
- Modify: `/Volumes/YC/AI产品/codex-mb/feishu_longconn_service.py`
- Modify: `/Volumes/YC/AI产品/codex-mb/tests/test_feishu_service.py`
- Modify: `/Users/aa/.config/codex-tg/feishu.env`

**Step 1: Write the failing test**

在 `/Volumes/YC/AI产品/codex-mb/tests/test_feishu_service.py` 添加 sidecar 行为测试，覆盖三件事：
- 普通消息在非私人助手工作区时，主回复和 sidecar 同时运行，但 cwd 不同。
- 当前 cwd 已经是私人助手工作区时，不重复 sidecar。
- sidecar 使用独立的 `personal_assistant` 辅助会话，不改写 `active_session_id/active_cwd`。

```python
def test_plain_message_runs_main_prompt_and_sidecar_sync(self) -> None:
    service = build_real_service_with_fake_runner(...)
    service.personal_assistant_config = PersonalAssistantConfig(
        cwd=Path("/tmp/assistant"),
        side_sync_enabled=True,
    )
    service.state.set_active_session("ou-owner", "main-1", "/tmp/project-a")

    service._run_prompt_worker(
        chat_id="chat-p2p",
        actor_id="ou-owner",
        prompt="今天把专利提纲补完",
        active_id="main-1",
        cwd=Path("/tmp/project-a"),
        session_label="main",
    )

    assert service.codex.calls[0][1] == "/tmp/project-a"
    assert service.codex.calls[1][1] == "/tmp/assistant"
    assert service.state.get_active("ou-owner") == ("main-1", "/tmp/project-a")
    assert service.state.get_aux_session("ou-owner", "personal_assistant")[1] == "/tmp/assistant"
```

**Step 2: Run test to verify it fails**

Run:
```bash
python3 -m pytest /Volumes/YC/AI产品/codex-mb/tests/test_feishu_service.py -v
```

Expected: FAIL，新增测试会报“没有 personal_assistant_config / 没有 get_aux_session / 没有第二次 codex 调用”。

**Step 3: Write minimal implementation**

在 `/Volumes/YC/AI产品/codex-mb/feishu_longconn_service.py` 中：
- 在 `build_service()` 阶段读取 `PERSONAL_ASSISTANT_CWD` 和 `PERSONAL_ASSISTANT_SIDE_SYNC_ENABLED`。
- 在 `_run_prompt_worker()` 主流程成功结束后，若当前 cwd 不是私人助手 cwd，则运行一次静默 sidecar prompt。
- sidecar 只写私人助手状态文件，不向飞书回发第二条消息。
- sidecar 会话通过 `BotState` 的 `personal_assistant` 命名空间持久化。

```python
assistant_cfg = load_personal_assistant_config(os.environ)
...
if assistant_cfg and assistant_cfg.side_sync_enabled and cwd != assistant_cfg.cwd:
    aux_session_id, _ = self.state.get_aux_session(actor_id, "personal_assistant")
    sidecar_prompt = build_sidecar_sync_prompt(
        user_text=prompt,
        source_cwd=cwd,
        skill_path=assistant_cfg.cwd / "skills" / "personal-assistant-chat" / "SKILL.md",
        state_dir=assistant_cfg.cwd / "state",
    )
    execution = run_prompt_with_workspace_write_recovery(
        self.codex,
        prompt=sidecar_prompt,
        cwd=assistant_cfg.cwd,
        session_id=aux_session_id,
    )
    if execution.thread_id:
        self.state.update_aux_session_if_unchanged(
            actor_id,
            "personal_assistant",
            aux_session_id,
            execution.thread_id,
            str(assistant_cfg.cwd),
        )
```

并在 `/Users/aa/.config/codex-tg/feishu.env` 增加：

```bash
PERSONAL_ASSISTANT_CWD="/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手"
PERSONAL_ASSISTANT_SIDE_SYNC_ENABLED=1
```

**Step 4: Run test to verify it passes**

Run:
```bash
python3 -m pytest /Volumes/YC/AI产品/codex-mb/tests/test_feishu_service.py -v
```

Expected: PASS，新增 sidecar 行为测试通过，原有 `/sessions`、`/use`、owner mode 用例不回归。

**Step 5: Commit**

```bash
git -C /Volumes/YC/AI产品/codex-mb add \
  /Volumes/YC/AI产品/codex-mb/feishu_longconn_service.py \
  /Volumes/YC/AI产品/codex-mb/tests/test_feishu_service.py

git -C /Volumes/YC/AI产品/codex-mb commit -m "feat: sync personal assistant state across workspaces"
```

### Task 3: 在私人助手工作区加入“协商式次日日程”状态与技能

**Files:**
- Modify: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/AGENTS.md`
- Modify: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/skills/personal-assistant-chat/SKILL.md`
- Modify: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/skills/personal-assistant-watch/SKILL.md`
- Create: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/skills/personal-assistant-schedule-draft/SKILL.md`
- Create: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/skills/personal-assistant-schedule-followup/SKILL.md`
- Create: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/daily_schedule.json`
- Modify: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/contracts.md`
- Modify: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/profile.yaml`

**Step 1: Write the failing test**

先写一个最小 schema 校验脚本，验证 `daily_schedule.json` 必须存在并包含 `draft_plan`、`confirmed_plan`、`slots` 和 `parent_task_id` 这些字段；在文件未创建前它应失败。

```bash
python3 - <<'PY'
from pathlib import Path
import json

path = Path('/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/daily_schedule.json')
payload = json.loads(path.read_text(encoding='utf-8'))
assert 'draft_plan' in payload
assert 'confirmed_plan' in payload
plan = payload['draft_plan'] or payload['confirmed_plan']
if plan:
    assert all('parent_task_id' in slot for slot in plan['slots'])
PY
```

Expected: FAIL，因为文件还不存在或字段不完整。

**Step 2: Run test to verify it fails**

Run 上面的 `python3` 校验脚本。

Expected: FAIL with `FileNotFoundError` 或 `AssertionError`。

**Step 3: Write minimal implementation**

新增 `daily_schedule.json` 初始结构，并扩展聊天/自动化 skill：
- `personal-assistant-chat` 新增 `daily_plan_confirm`、`daily_plan_adjust`、`daily_plan_progress` 三类输入。
- `personal-assistant-schedule-draft` 负责前一晚 22:00 从大任务生成“建议稿”，但只有在用户确认后才写入 `confirmed_plan`。
- `personal-assistant-schedule-followup` 负责在已确认时间点进入窗口后，温和问一句“现在进度怎么样”，收到回复后通过 `personal-assistant-chat` 回写大任务。
- `personal-assistant-watch` 继续负责原有风险提醒，不接管每日计划生成，以避免与原能力冲突。

`/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/daily_schedule.json` 初始建议：

```json
{
  "version": 1,
  "timezone": "Asia/Shanghai",
  "draft_plan": null,
  "confirmed_plan": null,
  "last_schedule_message_at": null
}
```

`draft_plan` / `confirmed_plan` 建议结构：

```json
{
  "date": "2026-04-11",
  "status": "draft",
  "generated_at": "2026-04-10T22:00:00+08:00",
  "awaiting_confirmation": true,
  "slots": [
    {
      "slot_id": "slot-0900",
      "planned_time": "2026-04-11T09:00:00+08:00",
      "label": "09:00",
      "parent_task_id": "personal-assistant-setup",
      "summary": "检查飞书消息是否已稳定同步到私人助手状态",
      "status": "proposed",
      "user_note": null,
      "last_follow_up_at": null
    }
  ]
}
```

在 `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/profile.yaml` 增加：

```yaml
daily_schedule_draft_time: "22:00"
daily_schedule_max_slots: 4
daily_schedule_follow_up_grace_minutes: 30
```

**Step 4: Run test to verify it passes**

Run:
```bash
python3 - <<'PY'
from pathlib import Path
import json

path = Path('/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/daily_schedule.json')
payload = json.loads(path.read_text(encoding='utf-8'))
assert payload['version'] == 1
assert 'draft_plan' in payload
assert 'confirmed_plan' in payload
print('ok')
PY
```

Expected: PASS，输出 `ok`。

**Step 5: Commit**

这个任务大多是工作区外部文件，不能直接进入当前 repo。若本任务同时在 repo 中新增了模板说明文件或辅助脚本，再提交 repo 文件；否则跳过 git commit，继续下一任务。

### Task 4: 用独立自动化接入“22:00 次日日程建议稿”和“次日进度追问”

**Files:**
- Modify: `/Users/aa/.codex/automations/automation/automation.toml`
- Create: `/Users/aa/.codex/automations/automation-3/automation.toml`
- Create: `/Users/aa/.codex/automations/automation-4/automation.toml`
- Optional Modify: `/Users/aa/.codex/automations/automation-2/automation.toml`

**Step 1: Write the failing test**

先用文件存在性和关键 prompt 文本做“配置失败验证”：在创建前，`automation-3` 和 `automation-4` 应该不存在。

```bash
test -f /Users/aa/.codex/automations/automation-3/automation.toml
test -f /Users/aa/.codex/automations/automation-4/automation.toml
```

Expected: 两条命令都返回非 0。

**Step 2: Run test to verify it fails**

Run 上述两条 `test -f`。

Expected: FAIL，因为两个自动化文件尚未创建。

**Step 3: Write minimal implementation**

保留现有 `automation` 作为“风险提醒/温和追问”自动化，不让它承担每日计划职责；新增两条自动化，避免与原功能冲突：

- `automation-3`：每天 22:00 运行，使用 `[$personal-assistant-schedule-draft]` 读取 `work_state.json` 与 `daily_schedule.json`，生成“次日日程建议稿”，发给飞书，等待用户确认。
- `automation-4`：每小时运行，使用 `[$personal-assistant-schedule-followup]` 只检查已确认的时间点是否进入跟进窗口，若需要则温和询问进度。

`/Users/aa/.codex/automations/automation-3/automation.toml` 建议：

```toml
version = 1
id = "automation-3"
kind = "cron"
name = "私人助手次日日程建议稿"
prompt = "使用[$personal-assistant-schedule-draft](.../skills/personal-assistant-schedule-draft/SKILL.md) 读取 state/profile.yaml、state/work_state.json、state/daily_schedule.json 和 state/contracts.md。根据长期大任务生成次日日程建议稿；必须等待用户确认后才能写入 confirmed_plan。若 assistant_mode=live，则通过 FeishuAPI.send_message_to_open_id(...) 发送建议稿。"
status = "ACTIVE"
rrule = "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR,SA,SU;BYHOUR=22;BYMINUTE=0"
execution_environment = "local"
cwds = ["/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手"]
```

`/Users/aa/.codex/automations/automation-4/automation.toml` 建议：

```toml
version = 1
id = "automation-4"
kind = "cron"
name = "私人助手日程跟进"
prompt = "使用[$personal-assistant-schedule-followup](.../skills/personal-assistant-schedule-followup/SKILL.md) 读取 state/profile.yaml、state/work_state.json、state/daily_schedule.json、state/reminder_log.jsonl 和 state/contracts.md。只检查已确认的日程时间点是否到达跟进窗口；若需要提醒，则温和询问当前进度，并通过 FeishuAPI.send_message_to_open_id(...) 发送。"
status = "ACTIVE"
rrule = "FREQ=HOURLY;INTERVAL=1"
execution_environment = "local"
cwds = ["/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手"]
```

**Step 4: Run test to verify it passes**

Run:
```bash
test -f /Users/aa/.codex/automations/automation-3/automation.toml && \
rg -n "personal-assistant-schedule-draft|BYHOUR=22|send_message_to_open_id" /Users/aa/.codex/automations/automation-3/automation.toml && \
test -f /Users/aa/.codex/automations/automation-4/automation.toml && \
rg -n "personal-assistant-schedule-followup|FREQ=HOURLY|send_message_to_open_id" /Users/aa/.codex/automations/automation-4/automation.toml
```

Expected: PASS，能匹配到技能名、调度规则和 `send_message_to_open_id`。

**Step 5: Commit**

自动化 TOML 位于 `~/.codex`，不在 repo 中，跳过 git commit；若同时修改了 repo 内的模板或说明文件，再单独提交 repo 变更。

### Task 5: 端到端验证“主回复不变、旁路同步生效、日程确认后才回写”

**Files:**
- Verify: `/Volumes/YC/AI产品/codex-mb/tests/test_feishu_service.py`
- Verify: `/Volumes/YC/AI产品/codex-mb/tests/test_codex_common.py`
- Verify: `/Volumes/YC/AI产品/codex-mb/tests/test_personal_assistant_bridge.py`
- Verify: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/work_state.json`
- Verify: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/daily_schedule.json`
- Verify: `/Users/aa/.codex/automations/automation/automation.toml`
- Verify: `/Users/aa/.codex/automations/automation-3/automation.toml`
- Verify: `/Users/aa/.codex/automations/automation-4/automation.toml`

**Step 1: Write the failing test**

补一个手工 smoke checklist，并先执行一次，确认在功能未完整接上前至少有一项失败：

```bash
python3 -m pytest \
  /Volumes/YC/AI产品/codex-mb/tests/test_personal_assistant_bridge.py \
  /Volumes/YC/AI产品/codex-mb/tests/test_codex_common.py \
  /Volumes/YC/AI产品/codex-mb/tests/test_feishu_service.py -v
```

Expected: 在功能未完全落地时，sidecar / schedule 相关新增测试至少有一项 FAIL。

**Step 2: Run test to verify it fails**

执行上述 `pytest`，记录失败用例名称，作为本轮收尾前必须归零的清单。

**Step 3: Write minimal implementation**

全部功能落地后，做这组手工验证：
- 在飞书把当前会话切到另一个工作区，发送一条任务更新消息。
- 确认主回复仍来自那个工作区。
- 确认私人助手工作区的 `work_state.json` 被 sidecar 更新。
- 人工触发一次 22:00 日程草案 prompt，确认发出的只是“建议稿”，`confirmed_plan` 尚未写入。
- 在飞书回复“把 14:00 那项改到 15:30，然后按这个执行”，确认 `daily_schedule.json` 从 `draft_plan` 变成 `confirmed_plan`。
- 再人工触发一次 schedule follow-up，确认它会基于已确认时间点发进度询问；用户回复后，再确认对应大任务的 `last_user_update_at` / `last_progress_at` 被更新。

**Step 4: Run test to verify it passes**

Run:
```bash
python3 -m pytest \
  /Volumes/YC/AI产品/codex-mb/tests/test_personal_assistant_bridge.py \
  /Volumes/YC/AI产品/codex-mb/tests/test_codex_common.py \
  /Volumes/YC/AI产品/codex-mb/tests/test_feishu_service.py -v
```

Expected: PASS，所有新增测试通过，且现有飞书会话切换能力未回归。

然后执行配置核对：

```bash
rg -n "PERSONAL_ASSISTANT_CWD|PERSONAL_ASSISTANT_SIDE_SYNC_ENABLED" /Users/aa/.config/codex-tg/feishu.env && \
rg -n "daily_schedule_draft_time|daily_schedule_follow_up_grace_minutes" '/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/profile.yaml' && \
python3 - <<'PY'
from pathlib import Path
import json
payload = json.loads(Path('/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/daily_schedule.json').read_text(encoding='utf-8'))
assert 'draft_plan' in payload
assert 'confirmed_plan' in payload
print('daily_schedule schema ok')
PY
```

Expected: PASS，并输出 `daily_schedule schema ok`。

**Step 5: Commit**

```bash
git -C /Volumes/YC/AI产品/codex-mb add \
  /Volumes/YC/AI产品/codex-mb/personal_assistant_bridge.py \
  /Volumes/YC/AI产品/codex-mb/codex_common.py \
  /Volumes/YC/AI产品/codex-mb/feishu_longconn_service.py \
  /Volumes/YC/AI产品/codex-mb/tests/test_personal_assistant_bridge.py \
  /Volumes/YC/AI产品/codex-mb/tests/test_codex_common.py \
  /Volumes/YC/AI产品/codex-mb/tests/test_feishu_service.py

git -C /Volumes/YC/AI产品/codex-mb commit -m "feat: add cross-workspace assistant sync and daily schedule flow"
```
