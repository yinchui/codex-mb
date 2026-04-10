# Personal Assistant Check-in Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 清理 live 验证残留，并为私人助手加入平衡型主动询问规则。

**Architecture:** 通过更新私人助手工作区中的 skill 文本、状态配置和自动化提示词，让 Codex 在后台巡检时先判断高优先级风险，再在低风险但长期沉默的场景下触发 `gentle_check_in`。不新增复杂持久化结构，优先复用 `last_user_update_at`、`last_ingested_message_at` 和 `reminder_log.jsonl`。

**Tech Stack:** Markdown skill files, YAML/JSON state files, Codex automation TOML

---

### Task 1: 写入文档并确认范围

**Files:**
- Create: `docs/plans/2026-04-10-personal-assistant-checkin-design.md`
- Create: `docs/plans/2026-04-10-personal-assistant-checkin-implementation.md`

**Step 1:** 写入简短设计文档。

**Step 2:** 写入实现计划文档。

**Step 3:** 复核文档中的触发条件、阈值和清理范围。

### Task 2: 清理测试残留

**Files:**
- Modify: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/work_state.json`
- Modify: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/notes.md`
- Modify: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/reminder_log.jsonl`

**Step 1:** 从 `work_state.json` 删除 `assistant-live-smoke-test`。

**Step 2:** 从 `notes.md` 删除影子测试备注。

**Step 3:** 从 `reminder_log.jsonl` 删除测试发送记录。

**Step 4:** 重新检查状态文件，确认只保留真实任务。

### Task 3: 加入平衡型主动询问规则

**Files:**
- Modify: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/skills/personal-assistant-watch/SKILL.md`
- Modify: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/skills/personal-assistant-chat/SKILL.md`
- Modify: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/profile.yaml`
- Modify: `/Users/aa/Nutstore Files/我的坚果云/我的坚果云/Codex私人助手/state/contracts.md`

**Step 1:** 在 watch skill 中加入 `gentle_check_in` 的触发顺序、阈值依据和语气要求。

**Step 2:** 在 chat skill 中明确：收到任务相关同步后刷新 `last_user_update_at`/`last_progress_at`，避免用户刚回复又被追问。

**Step 3:** 在 profile 中加入 `gentle_check_in_after_hours` 和 `gentle_check_in_cooldown_hours`。

**Step 4:** 在 contracts 中补充提醒原因枚举和推荐字段说明。

### Task 4: 同步自动化并验证

**Files:**
- Modify: `~/.codex/automations/automation/automation.toml`
- Modify: `~/.codex/automations/automation-2/automation.toml`

**Step 1:** 更新状态巡检自动化提示词，明确评估 `gentle_check_in`，并继续使用 `send_message_to_open_id(...)`。

**Step 2:** 同步更新周回顾自动化提示词，保持 live 发送方式一致。

**Step 3:** 重新读取 `profile.yaml`、`work_state.json`、`reminder_log.jsonl`、自动化 TOML，确认清理结果和新规则都已落盘。

**Step 4:** 在最终汇报中说明当前仍为 `live`，并列出后续真正会触发主动询问的条件。
