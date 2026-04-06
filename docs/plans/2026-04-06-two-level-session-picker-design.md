# Two-Level Session Picker Design

## Goal

把 `/sessions` 从“直接列最近会话”改成“两级选择”流程：先选工作区，再选该工作区下的具体对话。

## Current Problem

当前 `/sessions` 会直接列出最近会话，每一项末尾显示一个 `cwd` 名称。这个 `cwd` 只是显示字段，不是第一层“工作区选择”。因此手机端看到编号后，容易误以为自己先选的是工作区，随后还应该出现第二层对话记录列表，但实际上当前实现会直接切换到具体 session。

## Proposed Behavior

### Level 1: Workspace Picker

- `/sessions` 首先按 `cwd` 分组最近会话。
- 每个工作区只显示一次，并带上最近会话数。
- 用户回复工作区编号后，进入第二层会话选择。

### Level 2: Session Picker

- 第二层只显示所选工作区下的最近会话。
- 用户回复编号后，才真正切换到该 session。
- 即便该工作区下只有一个会话，也保留第二层，保持交互一致。

### Direct Access Compatibility

以下能力保持不变：

- `/use <session_id>` 仍可直接切换
- `/history` 仍只负责查看历史
- 普通消息续聊逻辑不变

## State Changes

当前状态只有一个 `pending_session_pick`，不足以表达“两级选择”。需要新增：

- `pending_workspace_pick`
- `workspace_picker`
- `session_picker` 继续保留，但只用于第二层

这样数字输入时，服务才能区分当前是在选工作区还是在选会话。

## Channel Scope

三端统一改动：

- Feishu
- Telegram
- WeChat

## Testing

新增或更新测试覆盖：

- `/sessions` 第一层显示工作区列表而不是直接显示 session
- 选择工作区后出现第二层 session 列表
- 选择第二层编号后切换到正确 session
- `/use <session_id>` 直达仍可用
- `/model`、附件选择等现有数字选择逻辑不被两级会话选择打乱
