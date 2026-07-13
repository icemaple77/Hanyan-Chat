# Hanyan-Chat 修复记录

## Bug 1: 主动消息级联触发
**症状**: 1分钟内触发5+次主动消息，每次发1-2条，房间被刷屏

**根因**: `_auto_message_loop` 线程在进程多次重启/重连时被启动了多个副本。
每个副本每30秒检查一次session，N个副本=N倍消息。

**修复**:
- `bot.py:347-349`: 加 `_auto_message_started` 守卫，防止重复启动
- `bot.py:378`: `last_active` 提前到 LLM 调用前更新

## Bug 2: `_is_dm` 限流耗尽
**症状**: `send_text failed: Too Many Requests`

**根因**: 每次 sync（5秒一次）都调 `get_joined_members` API，耗尽 Synapse 限流额度

**修复**:
- `matrix_client.py:88-90`: 加 DM 缓存（5分钟过期）
- `matrix_client.py:425-435`: `_is_dm` 先查缓存

## Bug 3: Synapse 限流过严
**症状**: 同上

**根因**: Synapse 默认限流每秒200次，bot 高频调用时被 ban

**修复**:
- `homeserver.yaml`: 限流提到每秒1000次

## Bug 4: 主动消息发给 @hermes
**症状**: 用户看到 @hermes 也收到主动消息

**根因**: session 管理器有 @hermes 的 session，主动消息发到同一个房间

**修复**:
- `bot.py:371`: 过滤 `@hermes:` 和 `@serena:` 的 session

## 代码逻辑（返回给 Claude Code 审查）
- `split_reply` → 只按 `[tickle]` 标记拆分，其余整段返回
- `_send_actions` → 遍历 actions，每个 action 发1条文字 + (第一条额外语音)
- `_send_proactive_message` → 独立发送循环，有 `_send_async` 同步阻塞
- 所有拆分函数 `_split_on_hard_boundaries`、`_split_single_backslash` 已废弃
