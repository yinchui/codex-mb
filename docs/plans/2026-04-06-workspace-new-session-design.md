# Workspace New Session Option Design

## Goal

在 `/sessions` 的第二层工作区会话列表中，新增“新建会话”选项，让用户可以先选工作区，再直接在该工作区下开启一个新线程。

## Desired Behavior

### Level 1: Workspace Picker

- `/sessions` 第一层仍然先列工作区。
- 用户选择工作区后，进入第二层。

### Level 2: Session Options

- 第二层顶部固定显示一个“新建会话”选项。
- 其后继续显示该工作区下的最近会话。
- 用户回复“新建会话”对应编号时，不切到旧 session，而是进入该工作区的 `/new` 模式。
- 用户回复其他编号时，仍切换到对应历史 session。

## Compatibility

- `/use <session_id>` 保持直达历史 session，不负责“新建会话”。
- `/history` 保持不变。
- 普通消息续聊逻辑保持不变。

## State Changes

当前第二层只依赖 `last_session_ids`，不足以表达“新建会话”这种非 session 选项。需要新增一个真正的第二层 picker：

- `session_picker`
- 每个选项包含：
  - `kind`: `new` 或 `session`
  - `cwd`
  - `session_id`（仅 `session` 选项需要）

这样数字回复时，服务才能区分“切旧会话”还是“在该工作区新建会话”。

## Channel Scope

三端统一：

- Feishu
- Telegram
- WeChat

## Testing

新增或更新测试覆盖：

- 选择工作区后，第二层第一项是“新建会话”
- 选择“新建会话”后，active session 为空，但 active cwd 变为该工作区
- Telegram 第二层按钮也包含“新建会话”
- 选择历史会话的现有行为不回归
