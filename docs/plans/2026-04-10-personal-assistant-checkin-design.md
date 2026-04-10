# 私人助手平衡型主动询问设计

**目标**

在不增加明显打扰感的前提下，让飞书私人助手在 `live` 模式下能够在“需要的时候”温和地主动询问用户近况，而不是只在风险事件发生时提醒。

**设计原则**

- 继续保留现有风险提醒优先级：截止风险、高优先级停滞、等待超时、任务过载。
- 新增一层轻量的 `gentle_check_in` 触发，用于“没有显著风险，但用户沉默较久且仍有活跃任务”的场景。
- `gentle_check_in` 只在没有更高优先级提醒理由命中时生效。
- `gentle_check_in` 必须受静默时间和冷却窗口约束，避免高频追问。
- 用户一旦通过飞书同步任务近况，应刷新相应时间戳，从而自然抑制后续追问。

**平衡型规则**

- 候选条件：存在至少一个 `active` 任务。
- 时间依据：优先参考任务的 `last_user_update_at`，回退到全局 `last_ingested_message_at`。
- 触发阈值：默认超过 `24` 小时未同步近况时，允许发起一次温和询问。
- 冷却阈值：同一任务同一理由默认 `24` 小时内不重复提醒。
- 语气要求：提醒文案以“同步近况”“我可以帮你拆下一步”为主，不使用催促式表达。

**清理策略**

本次 live 冒烟验证遗留的测试状态不应继续污染后续巡检，因此需要清理：

- `work_state.json` 中的测试任务
- `notes.md` 中的影子测试备注
- `reminder_log.jsonl` 中的测试发送记录

**实施点**

- 更新 `skills/personal-assistant-watch/SKILL.md`
- 更新 `skills/personal-assistant-chat/SKILL.md`
- 更新 `state/profile.yaml`
- 视需要补充 `state/contracts.md`
- 更新 `~/.codex/automations/automation/automation.toml`
- 同步收紧周回顾自动化提示词，保持发送方式一致
