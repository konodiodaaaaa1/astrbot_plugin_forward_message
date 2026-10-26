# 更新日志

本文件记录 QQ 合并转发助手各版本的重要变更。

## [2.2.0] - 2026-10-26

### 新增

- 读取类 LLM Tool 改为返回 JSON 结构化结果：含 node_count、max_depth、fully_expanded、unresolved_ids 与逐层 nodes，嵌套状态不再压成一行文本；同时附 text_preview 便于直接引用。
- 不引用也能读：没有候选时扫描最近历史，取最新一条合并转发消息读取；历史扫描带分页，命中内联内容优先于协议端 res_id 解析。
- 历史内联内容索引在嵌套展开时复用：外层内联内容里带的嵌套转发 ID 先查索引再打协议端，减少无谓往返。
- 本插件发出的合并转发按 message_id 落本地缓存（data/plugin_data 下，上限 200 条），之后被引用读取时直接还原完整嵌套树，不依赖协议端能否取回 res_id。
- 解析器支持 reply/json/xml 段：嵌套转发里"引用某条消息"的节点不再被当成空节点跳过。

### 修复

- 废弃群聊/私聊的分开限定：私聊走 send_private_forward_msg，群聊走 send_group_forward_msg，不再拒绝私聊。
- 取消"发送者必须是当前群成员"的硬校验，取不到资料时退回 QQ号占位名；群白名单仍只作用于群聊。

## [2.1.4] - 2026-10-26

### 修复

- 读取侧不再要求群聊：私聊里引用转发也能读，发送仍然只允许群聊。
- 私聊读取时预热与历史回退改用 get_friend_msg_history。
- 转发段同时带 id 与内联 content 时优先用 content（parseMultMsg=true 时可直接省掉协议端往返）。
- 历史回退在"只找到一条带内容的转发"时直接采用，解决引用回复短 id 与转发段 id 不同号导致的匹配失败。
- 转发段同时带 res_id 时优先用 res_id（NapCat 对数字 msgId 常报"内层消息"，res_id 是字符串可走 protocolFallbackLogic）；转发 ID 校验放宽到 base64 字符集。
- 拉到的应答必须先确认含非空节点列表才算成功，避免空壳应答提前返回导致"转发内容为空"。

## [2.1.3] - 2026-10-26

### 修复

- 定位到根因：NapCat 在本机配置（parseMultMsg=false）下把合并转发表示成 com.tencent.multimsg 的 json 卡片，且 get_forward_msg 只接受 res_id 字符串，数字 message_id 与 19 位 msgId 都会报"消息已过期或者为内层消息"。
- 候选 ID 优先取 OneBot 原始消息段里的 reply 短 id 与 json 卡片的 resid，再退回显式参数和 AstrBot 组件。
- 取不到时用 get_msg 拿到消息段，解析 json 卡片里的 resid 再调 get_forward_msg(id=resid)；本机实测该路径返回正常。

## [2.1.2] - 2026-10-26

### 修复

- 新增第四层回退：按 ID 全部失败后，扫最近 50/150 条群历史，从中找带内联内容的转发段（群历史接口默认 parse_mult_msg=true，会把转发内容直接带出来）。
- 引用回复的链路里若含合并转发 json 卡片，解析其中的 resid 作为候选 ID。
- 失败日志额外输出消息组件、Reply.chain 内容与全部候选 ID，便于定位协议端拒绝的是哪一个。

## [2.1.1] - 2026-10-26

### 修复

- 读取引用转发时改用多候选 ID + 多策略回退：先试 get_forward_msg 的 message_id / id，再通过 get_msg 从消息段里提取真实转发 ID（含 multimsg JSON 的 resid），最后拉一次群消息历史预热 ID 映射再重试。
- 读取失败时把底层报错一并返回并写入日志，不再只剩一句"读取失败"。
- 新增 /转发调试，输出候选 ID 与每种调用方式的实际返回。

## [2.1.0] - 2026-10-26

### 新增

- 转发节点支持嵌套转发：JSON 里的 {"type":"node",...}、{"type":"forward","id":"..."} 与行内 [forward:ID] 三种写法，发送前递归展开。
- 新增 /查看转发：递归读取一条合并转发（含嵌套层）并输出缩进文本。
- 新增 /转发复制：读取引用到的转发后原样重发，保留嵌套层。
- 新增 read_qq_forward_message LLM Tool。
- 新增 /转发调试：输出候选转发 ID 与每种调用方式的实际返回，便于定位协议端拒绝的原因。

### 修复

- 修复转发里再套转发时读不出来的问题：此前 content 只接受 text/at/image/face，遇到 node/forward 段直接报"不支持的消息段类型"；读取侧完全没有实现。

### 边界

- 嵌套转发带环检测、层数上限、节点总量预算和拉取次数上限，均可在配置里调整。
- 一个节点里混排"普通消息 + 转发"时 QQ 无嵌套 node 表达方式，改用带 ID 的 forward 段，由 NapCat 转成 multimsg；没有 ID 的混排结构会被拒绝。

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
