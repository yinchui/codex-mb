# 私人助手 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 把 Codex 配置成一个以飞书私聊为主入口的私人助手，能够更新用户任务状态、维护长期记忆，并通过自动化按需发送飞书提醒。

**Architecture:** 方案优先复用现有飞书网关和 Codex 自动化能力，不先重写当前仓库。私人助手的行为规则、skills 和动态状态都放在坚果云中的独立工作区；飞书入口通过默认工作区进入该目录；自动化周期性读取状态文件并按条件决定是否主动提醒。

**Tech Stack:** Codex skills、Codex memory、Codex automations、Feishu long-connection bot、Markdown/YAML/JSON 状态文件、少量 Python 校验脚本

---

### Task 1: 创建私人助手工作区骨架

**Files:**
- Create: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/AGENTS.md`
- Create: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/skills/personal-assistant-chat/SKILL.md`
- Create: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/skills/personal-assistant-watch/SKILL.md`
- Create: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/profile.yaml`
- Create: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/work_state.json`
- Create: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/reminder_log.jsonl`
- Create: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/notes.md`
- Create: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/docs/plans/.gitkeep`

**Step 1: 写一个失败的存在性检查**

```bash
test -f "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/AGENTS.md"
```

**Step 2: 运行检查并确认失败**

Run:

```bash
test -f "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/AGENTS.md"; echo $?
```

Expected: 输出 `1`

**Step 3: 创建目录与空白种子文件**

```bash
mkdir -p \
  "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/skills/personal-assistant-chat" \
  "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/skills/personal-assistant-watch" \
  "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state" \
  "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/docs/plans"

cat <<'EOF' > "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/profile.yaml"
assistant_mode: shadow
reminder_style: balanced
primary_channel: feishu_p2p
follow_up_after_hours: 48
high_priority_stale_hours: 48
waiting_timeout_hours: 72
deadline_risk_hours: 24
max_active_tasks: 3
quiet_hours:
  start: "22:30"
  end: "08:30"
owner_open_id: ""
EOF

cat <<'EOF' > "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/work_state.json"
{
  "version": 1,
  "tasks": [],
  "updated_at": null
}
EOF

: > "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/reminder_log.jsonl"
: > "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/notes.md"
: > "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/AGENTS.md"
: > "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/skills/personal-assistant-chat/SKILL.md"
: > "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/skills/personal-assistant-watch/SKILL.md"
: > "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/docs/plans/.gitkeep"
```

**Step 4: 重新运行检查并确认通过**

Run:

```bash
test -f "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/AGENTS.md" && \
test -f "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/profile.yaml" && \
echo ok
```

Expected: 输出 `ok`

**Step 5: Commit**

```bash
git add "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手"
git commit -m "chore: scaffold personal assistant workspace"
```

### Task 2: 把私人助手工作区加入 Codex 工作区列表

**Files:**
- Modify: `/Users/aa/.codex/.codex-global-state.json`

**Step 1: 写一个失败检查，确认当前未注册该工作区**

```bash
python3 - <<'PY'
import json, pathlib, sys
p = pathlib.Path('/Users/aa/.codex/.codex-global-state.json')
data = json.loads(p.read_text())
roots = data.get('electron-saved-workspace-roots', [])
target = '/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手'
sys.exit(0 if target in roots else 1)
PY
```

**Step 2: 运行检查并确认失败**

Run:

```bash
python3 - <<'PY'
import json, pathlib
p = pathlib.Path('/Users/aa/.codex/.codex-global-state.json')
data = json.loads(p.read_text())
print('/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手' in data.get('electron-saved-workspace-roots', []))
PY
```

Expected: 输出 `False`

**Step 3: 最小修改全局状态文件，加入工作区**

```python
import json
from pathlib import Path

path = Path("/Users/aa/.codex/.codex-global-state.json")
data = json.loads(path.read_text())
target = "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手"
roots = data.setdefault("electron-saved-workspace-roots", [])
if target not in roots:
    roots.append(target)
path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
```

**Step 4: 重新运行检查并确认通过**

Run:

```bash
python3 - <<'PY'
import json, pathlib
p = pathlib.Path('/Users/aa/.codex/.codex-global-state.json')
data = json.loads(p.read_text())
print('/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手' in data.get('electron-saved-workspace-roots', []))
PY
```

Expected: 输出 `True`

**Step 5: Commit**

```bash
git add /Users/aa/.codex/.codex-global-state.json
git commit -m "chore: register personal assistant workspace"
```

### Task 3: 让飞书私聊默认进入私人助手工作区

**Files:**
- Modify: `/Users/aa/.config/codex-tg/feishu.env`

**Step 1: 写一个失败检查，确认当前默认目录不是私人助手工作区**

```bash
rg -n '^DEFAULT_CWD=' /Users/aa/.config/codex-tg/feishu.env
```

**Step 2: 运行检查并确认需要改动**

Run:

```bash
python3 - <<'PY'
from pathlib import Path
for line in Path('/Users/aa/.config/codex-tg/feishu.env').read_text().splitlines():
    if line.startswith('DEFAULT_CWD='):
        print(line)
PY
```

Expected: 输出的 `DEFAULT_CWD` 不是 `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手`

**Step 3: 仅替换 DEFAULT_CWD**

```bash
python3 - <<'PY'
from pathlib import Path
path = Path('/Users/aa/.config/codex-tg/feishu.env')
target = 'DEFAULT_CWD="/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手"'
lines = path.read_text().splitlines()
updated = []
replaced = False
for line in lines:
    if line.startswith('DEFAULT_CWD='):
        updated.append(target)
        replaced = True
    else:
        updated.append(line)
if not replaced:
    updated.append(target)
path.write_text("\n".join(updated) + "\n")
PY
```

**Step 4: 重启飞书服务并验证新会话 cwd**

Run:

```bash
cd /Volumes/YC/AI产品/codex-mb && ./run_feishu.sh restart && ./run_feishu.sh status
```

Expected: 服务重启成功；随后在飞书私聊执行 `/new`，返回消息中的 `cwd` 是私人助手工作区

**Step 5: Commit**

```bash
git add /Users/aa/.config/codex-tg/feishu.env
git commit -m "chore: point feishu default cwd to personal assistant"
```

### Task 4: 写入私人助手工作区 AGENTS 规则

**Files:**
- Modify: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/AGENTS.md`

**Step 1: 写一个失败检查**

```bash
test -s "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/AGENTS.md"
```

**Step 2: 运行检查并确认失败**

Run:

```bash
test -s "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/AGENTS.md"; echo $?
```

Expected: 输出 `1`

**Step 3: 写入最小可用规则**

```markdown
永远用中文进行回复。

你当前运行在“私人助手工作区”中。

每次收到飞书私聊时，先读取以下文件：
- `state/profile.yaml`
- `state/work_state.json`
- `state/reminder_log.jsonl`

工作原则：
- 先判断用户消息是否在更新任务状态
- 新任务、进展、卡点、截止时间、偏好变化要被结构化吸收
- 不确定时最多追问一个关键问题
- 长期稳定偏好才写入 memory
- 当前任务状态只写入 `state/work_state.json`
- 主动提醒前必须检查静默时间、冷却窗口和最近提醒历史
- 若当前无需提醒，则安静结束，不为“定时”而提醒
```

**Step 4: 重新运行检查并确认通过**

Run:

```bash
test -s "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/AGENTS.md" && echo ok
```

Expected: 输出 `ok`

**Step 5: Commit**

```bash
git add "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/AGENTS.md"
git commit -m "docs: add personal assistant workspace rules"
```

### Task 5: 实现 `personal-assistant-chat` skill

**Files:**
- Modify: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/skills/personal-assistant-chat/SKILL.md`
- Test: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/docs/plans/chat-skill-smoke-cases.md`

**Step 1: 写一个失败检查**

```bash
test -s "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/skills/personal-assistant-chat/SKILL.md"
```

**Step 2: 运行检查并确认失败**

Run:

```bash
test -s "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/skills/personal-assistant-chat/SKILL.md"; echo $?
```

Expected: 输出 `1`

**Step 3: 写入最小 skill 内容**

```markdown
---
name: personal-assistant-chat
description: 处理飞书私聊中的用户消息，更新任务状态，必要时追问一个关键问题。
---

## 目标

- 把飞书对话转成结构化任务状态
- 抽取长期稳定偏好并写入 memory
- 只在必要时追问一个问题

## 每次执行必须做的事

1. 读取 `state/profile.yaml`、`state/work_state.json`、`state/reminder_log.jsonl`
2. 判断用户消息属于：新任务 / 进展 / 卡点 / 截止时间 / 偏好变化 / 闲聊
3. 更新 `work_state.json`
4. 如果用户表达了长期稳定偏好，写入 memory
5. 如果缺关键字段，最多追问一个问题
6. 回答要短，优先给出当前建议的下一步

## 不要做的事

- 不要把高频任务细节写进 memory
- 不要在同一条回复中追问多个问题
- 不要为了显得主动而强行提醒
```

**Step 4: 创建 3 个 smoke cases 并人工验证**

```markdown
# Chat Skill Smoke Cases

1. “这周我要把飞书提醒做完，优先级高”
   - 预期：新增一个高优任务

2. “这个先暂停，下周再说”
   - 预期：对应任务改成 paused

3. “别晚上提醒我”
   - 预期：偏好进入 memory 或 profile，而不是任务列表
```

Run:

```bash
test -s "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/skills/personal-assistant-chat/SKILL.md" && \
test -s "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/docs/plans/chat-skill-smoke-cases.md" && \
echo ok
```

Expected: 输出 `ok`

**Step 5: Commit**

```bash
git add \
  "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/skills/personal-assistant-chat/SKILL.md" \
  "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/docs/plans/chat-skill-smoke-cases.md"
git commit -m "docs: add chat skill for personal assistant"
```

### Task 6: 让 chat skill 跑通影子模式

**Files:**
- Modify: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/work_state.json`
- Modify: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/notes.md`

**Step 1: 写一个失败检查，确认状态仍为空**

```bash
python3 - <<'PY'
import json, pathlib, sys
data = json.loads(pathlib.Path('/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/work_state.json').read_text())
sys.exit(0 if data.get('tasks') else 1)
PY
```

**Step 2: 运行检查并确认失败**

Run:

```bash
python3 - <<'PY'
import json, pathlib
data = json.loads(pathlib.Path('/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/work_state.json').read_text())
print(len(data.get('tasks', [])))
PY
```

Expected: 输出 `0`

**Step 3: 在飞书私聊做 3 条真实对话演练**

```text
1. 我现在在做私人助手方案，优先级高
2. 这件事卡在怎么接飞书提醒
3. 别在晚上提醒我
```

要求：

- 不真的发主动提醒
- 只更新状态与长期偏好

**Step 4: 验证状态发生变化**

Run:

```bash
python3 - <<'PY'
import json, pathlib
data = json.loads(pathlib.Path('/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/work_state.json').read_text())
print(json.dumps(data, ensure_ascii=False, indent=2))
PY
```

Expected: 至少存在 1 个任务，且包含 `priority` 或 `status` 等关键字段

**Step 5: Commit**

```bash
git add \
  "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/work_state.json" \
  "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/notes.md"
git commit -m "test: seed assistant state from feishu shadow conversations"
```

### Task 7: 实现 `personal-assistant-watch` skill

**Files:**
- Modify: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/skills/personal-assistant-watch/SKILL.md`
- Create: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/docs/plans/watch-skill-cases.md`

**Step 1: 写一个失败检查**

```bash
test -s "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/skills/personal-assistant-watch/SKILL.md"
```

**Step 2: 运行检查并确认失败**

Run:

```bash
test -s "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/skills/personal-assistant-watch/SKILL.md"; echo $?
```

Expected: 输出 `1`

**Step 3: 写入最小 skill 内容**

```markdown
---
name: personal-assistant-watch
description: 周期性巡检任务状态，判断是否需要主动追问或提醒。
---

## 目标

- 读取当前状态
- 判断是否存在值得打扰用户的事项
- 若无需提醒则安静结束

## 判断顺序

1. 是否处于静默时间
2. 是否存在 deadline 风险
3. 是否存在高优任务长期无更新
4. 是否存在 waiting 超时
5. 是否存在 active 任务过多
6. 是否已在冷却窗口内提醒过同一事项

## 输出要求

- 若不提醒：给出简短内部说明，不发飞书
- 若提醒：只生成一条短消息
- 消息必须包含事项、原因、建议下一步
```

**Step 4: 写 3 个 watch cases 并人工验证**

```markdown
# Watch Skill Cases

1. 高优任务 48 小时未更新
   - 预期：触发提醒

2. 同一任务 2 小时前刚提醒过
   - 预期：跳过

3. 当前在静默时间且无 deadline 风险
   - 预期：跳过
```

Run:

```bash
test -s "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/skills/personal-assistant-watch/SKILL.md" && \
test -s "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/docs/plans/watch-skill-cases.md" && \
echo ok
```

Expected: 输出 `ok`

**Step 5: Commit**

```bash
git add \
  "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/skills/personal-assistant-watch/SKILL.md" \
  "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/docs/plans/watch-skill-cases.md"
git commit -m "docs: add watch skill for personal assistant"
```

### Task 8: 创建影子模式巡检自动化

**Files:**
- Create: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/docs/plans/assistant-watch-automation.md`

**Step 1: 写一个失败检查**

```bash
test -f "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/docs/plans/assistant-watch-automation.md"
```

**Step 2: 运行检查并确认失败**

Run:

```bash
test -f "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/docs/plans/assistant-watch-automation.md"; echo $?
```

Expected: 输出 `1`

**Step 3: 写出自动化规格**

```markdown
# assistant-watch automation

名称：私人助手状态巡检

工作区：
- `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手`

频率：
- 每小时一次

提示词要求：
- 使用 `personal-assistant-watch`
- 读取 `state/profile.yaml`、`state/work_state.json`、`state/reminder_log.jsonl`
- 如果只是在影子模式，禁止真的发飞书
- 只把“本来会提醒什么”写入 inbox 或日志
```

**Step 4: 用自动化工具创建影子模式自动化并验证**

Run:

```text
使用 automation_update 创建一个 cron 自动化：
- 名称：私人助手状态巡检
- 工作区：/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手
- 频率：每小时一次
- 模式：ACTIVE
- 当前为影子模式，不真的发飞书
```

Expected: 自动化在 Codex 中可见，且下次运行时间已生成

**Step 5: Commit**

```bash
git add "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/docs/plans/assistant-watch-automation.md"
git commit -m "docs: specify shadow watch automation"
```

### Task 9: 创建周回顾自动化规格

**Files:**
- Create: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/docs/plans/assistant-weekly-review-automation.md`

**Step 1: 写一个失败检查**

```bash
test -f "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/docs/plans/assistant-weekly-review-automation.md"
```

**Step 2: 运行检查并确认失败**

Run:

```bash
test -f "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/docs/plans/assistant-weekly-review-automation.md"; echo $?
```

Expected: 输出 `1`

**Step 3: 写出自动化规格**

```markdown
# assistant-weekly-review automation

名称：私人助手周回顾

工作区：
- `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手`

频率：
- 每周一次，工作日上午

提示词要求：
- 汇总本周 active、blocked、waiting 任务
- 标记长期停滞项
- 形成一条短飞书消息
- 在正式模式之前保持 PAUSED
```

**Step 4: 用自动化工具创建并保持暂停**

Run:

```text
使用 automation_update 创建一个 cron 自动化：
- 名称：私人助手周回顾
- 工作区：/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手
- 频率：每周一次
- 模式：PAUSED
```

Expected: 自动化在 Codex 中可见，状态为暂停

**Step 5: Commit**

```bash
git add "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/docs/plans/assistant-weekly-review-automation.md"
git commit -m "docs: specify weekly review automation"
```

### Task 10: 从影子模式升级到正式模式

**Files:**
- Modify: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/profile.yaml`
- Modify: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/reminder_log.jsonl`

**Step 1: 写一个失败检查，确认仍在影子模式**

```bash
rg -n '^assistant_mode: shadow$' "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/profile.yaml"
```

**Step 2: 运行检查并确认存在**

Run:

```bash
rg -n '^assistant_mode:' "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/profile.yaml"
```

Expected: 输出 `assistant_mode: shadow`

**Step 3: 把模式改为 live，并让 watch skill 允许发飞书**

```yaml
assistant_mode: live
reminder_style: balanced
primary_channel: feishu_p2p
follow_up_after_hours: 48
high_priority_stale_hours: 48
waiting_timeout_hours: 72
deadline_risk_hours: 24
max_active_tasks: 3
quiet_hours:
  start: "22:30"
  end: "08:30"
owner_open_id: "从 owner_state.json 自动读取或人工回填"
```

**Step 4: 用一次受控场景验证**

Run:

```text
人工制造一个高优任务长时间无更新的场景，等待下一次 `assistant-watch` 自动化运行。
```

Expected: 收到 1 条飞书私聊提醒；`state/reminder_log.jsonl` 增加一条记录

**Step 5: Commit**

```bash
git add \
  "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/profile.yaml" \
  "/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/reminder_log.jsonl"
git commit -m "feat: enable live reminders for personal assistant"
```

## 后续增强，不在第一轮必做

- 如果 `DEFAULT_CWD` 对其他飞书使用场景影响过大，再回到当前仓库增加“按 owner 绑定默认工作区”的能力
- 若坚果云同步频繁改写状态文件带来冲突，再把 `state/` 迁回本地目录，把 `skills/` 和文档继续留在坚果云
- 若后续接入日历、Obsidian、待办系统，再引入单独的同步 skill，而不是先做常驻服务
