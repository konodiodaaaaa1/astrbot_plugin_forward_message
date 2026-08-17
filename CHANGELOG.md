# 更新日志

本文件记录 QQ 合并转发助手各版本的重要变更。

## [2.0.4] - 2026-08-17

### 修复

- 将插件作者命名空间设为 `konodiodaaaaa1`，符合 AstrBot Cloud 插件标识规范。
- 原项目作者署名继续保留在 README 的来源与许可章节。

## [2.0.3] - 2026-08-17

### 变更

- 插件标识更名为 `astrbot_plugin_forward_message`。
- 展示名称更名为“QQ 合并转发助手”。
- 仓库地址更新为 `konodiodaaaaa1/astrbot_plugin_forward_message`。

## [2.0.2] - 2026-07-30

### 新增

- `/伪造转发` 和 `/伪造消息` 支持使用真实 QQ At 组件代替发送者 QQ 号。
- JSON、逐行 DSL 和旧命令输入支持使用 AstrBot `[At:QQ号]` 占位符作为发送者 ID。

### 修复

- 修复 At 组件后的文本段没有前导空格时，QQ 号与消息正文被错误拼接的问题。
- CQ 图片和表情消息段在发送 NapCat 合并转发节点前改由 AstrBot 消息组件完成转换。
- 本地 CQ 图片仅允许使用 AstrBot 临时目录内的文件。

### 兼容性

- At 转换仅在两个 SessionFaker 指令中执行，不影响普通群消息或 LLM Tool。
- 发送者 ID 和 At 目标仍执行原有的数字格式校验与当前群成员校验。

## [2.0.0] - 2026-07-30

### 新增

- 面向 AstrBot 4.26.8+ 和 AIOCQHTTP/NapCat 重构 SessionFaker。
- `/伪造转发` 支持 JSON 和逐行 DSL 输入。
- 保留可配置开关的旧版 `/伪造消息` 兼容入口。
- 新增受控的 `send_qq_forward_message` LLM Tool。
- 新增当前群路由、群成员校验、权限模式、群白名单和可配置输入上限。

[2.0.4]: https://github.com/konodiodaaaaa1/astrbot_plugin_forward_message/compare/v2.0.3...v2.0.4
[2.0.3]: https://github.com/konodiodaaaaa1/astrbot_plugin_forward_message/compare/v2.0.2...v2.0.3
[2.0.2]: https://github.com/konodiodaaaaa1/astrbot_plugin_forward_message/compare/v2.0.0...v2.0.2
[2.0.0]: https://github.com/konodiodaaaaa1/astrbot_plugin_forward_message/releases/tag/v2.0.0
