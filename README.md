# QQ 合并转发助手

QQ 合并转发助手是面向 AstrBot 4.26.8+ 和 AIOCQHTTP/NapCat 的 QQ 合并转发插件。
它支持命令和受控 LLM Tool，不使用外部 QQ 昵称 API，也不接受目标群号。

## 安全边界

- 群聊与私聊均可用，不再做会话类型限制；发送目标始终是触发命令或 Tool 的当前会话（群聊发群、私聊发对方）。
- 伪造发送者与 `at` 目标不再要求是群成员：能取到群名片/昵称就用，取不到退回 QQ号占位名。
- 昵称来自当前群名片，其次是 QQ 昵称；显式显示名仅作为展示覆盖。
- 可配置群白名单和 AstrBot/QQ群管理员权限。
- 节点、消息段、文本和图片均有可配置上限。
- 嵌套转发可无限层，但带环检测、节点总量预算和拉取次数上限。
- 图片只接受 HTTP/HTTPS URL。

## 安装

在 AstrBot 插件管理页上传发布 ZIP。ZIP 根目录已经是插件根目录，不需要额外解压一层。

## 新命令

JSON 格式：

```text
/伪造转发 [{"sender_id":"123456","content":"你好"},{"sender_id":"654321","sender_name":"显示名","content":[{"type":"text","text":"看这里"},{"type":"at","qq":"123456"},{"type":"image","url":"https://example.com/a.png"},{"type":"face","id":14}]}]
```

逐行 DSL：

```text
/伪造转发 123456[显示名]: 你好[at:654321]
654321: [image:https://example.com/a.png][face:14]
```

JSON 字符串内容和逐行 DSL 都支持 `[at:QQ号]`、`[image:https://...]` 和 `[face:ID]`。JSON/Tool 字符串还兼容 OneBot CQ 码 `[CQ:image,file=...]` 与 `[CQ:face,id=14]`；CQ 图片可以使用 AstrBot `data/temp` 中的本地绝对路径。在 `[` 前加反斜杠可输出字面标记，例如 `\[face:14]`。图片在发送前会由 AstrBot 转换为 NapCat 合并转发可用的格式。

在 `/伪造转发` 命令中，发送者 QQ 号和 JSON 的 `sender_id` 可以使用 AstrBot 的 `[At:QQ号]` 占位符，例如 `/伪造转发 [At:123456]: 内容`。真实 `At` 消息组件同样按其 QQ 号处理。转换只在相关命令被触发后执行，不影响普通群消息和 LLM Tool。小写的 `[at:QQ号]` 仍表示伪造消息内容中的 At 消息段。

## 嵌套转发

三种写法可以任意组合，结构本身没有层数上限（实际层数由 NapCat 发送链路决定，4.18 的 packet 路径在第 3 层后停止继续展开）：

1. 直接内联节点：`content` 里放一个 `node` 段。
2. 引用已有转发 ID：`content` 里放 `{"type":"forward","id":"转发ID"}`，发送前会递归拉取并展开。
3. 行内标记：字符串内容里写 `[forward:转发ID]`。

```text
/伪造转发 [{"sender_id":"123456","content":[{"type":"node","sender_id":"654321","sender_name":"显示名","content":[{"type":"text","text":"内层消息"}]}]}]
```

QQ 的合并转发里，一个节点要么整条是转发，要么整条是普通消息 —— NapCat 会跳过混排的节点。所以纯转发节点请让 `content` 只有一个转发段；如果一条消息里既有文字又想带转发，需要用带 ID 的 `forward` 段（插件会保留 ID 并交给 NapCat 转成 multimsg），混排且没有 ID 的结构会被直接拒绝。

## 读取与复制

`/查看转发` 递归读取一条合并转发并输出缩进文本，适合先确认结构再决定怎么处理：

```text
/查看转发          # 引用一条合并转发消息后发送
/查看转发 123456789  # 也可以直接给转发 ID
```

`/转发复制` 读取同一条转发后，把它原样重发到当前群，嵌套层一起带过去：

```text
/转发复制
```

读取失败时会依次尝试 `get_forward_msg(message_id)`、`get_forward_msg(id)`、`get_msg` 提取转发 ID、以及拉取一次群消息历史预热 ID 映射；仍失败时会把前两条底层报错一起返回。要看清每一步的实际返回，用 `/转发调试`（同样支持引用转发后发送）。

复制走的是"读取 → 重新构造"链路，因此嵌套层会被拆开重建；内层转发的发送者校验不参与当前群成员检查，只为顶层节点解析群名片。

## 旧命令

默认保留兼容入口，可在配置中关闭：

```text
/伪造消息 123456 第一条消息 | 654321 第二条消息
```

旧命令的发送者 QQ 号同样可以使用 `[At:QQ号]`，例如 `/伪造消息 [At:123456] 第一条消息`。

旧命令中的原消息图片按出现位置分配给对应节点。

## LLM Tool

插件注册两个 Tool：

- `send_qq_forward_message`：参数 `nodes_json` 使用与新命令相同的 JSON 节点数组，支持嵌套 `node` 段和 `{"type":"forward","id":...}`；`prompt`、`summary` 和 `source` 可选。Tool 不提供群号参数，因此模型不能跨会话路由消息。
- `read_qq_forward_message`：读取合并转发并返回 **JSON 结构化结果**（`type=forward_message`、`node_count`、`max_depth`、`fully_expanded`、`unresolved_ids`、逐层 `nodes`，`segments` 里 `type=forward` 的项带 `expanded` 与子 `nodes`），另附 `text_preview` 摘要；`forward_id` 留空时依次尝试当前消息携带的转发、引用回复的目标、最近一条转发消息。

## 配置

`permission_mode` 默认为 `everyone`，还支持：

- `astrbot_admin`
- `qq_group_admin`
- `admin_only`（满足任一种管理员身份）

`group_whitelist` 留空表示不限制群，多个群号用逗号或空格分隔。

嵌套转发相关配置：

| 配置项 | 默认值 | 说明 |
|---|---:|---|
| `max_forward_depth` | 0 | 嵌套最大层数，0 表示不限制 |
| `max_total_nodes` | 200 | 整棵转发树的节点总数上限 |
| `max_forward_fetch` | 32 | 一次操作里最多拉取多少次转发内容 |
| `enable_read_command` | true | 启用 `/查看转发` |
| `enable_copy_command` | true | 启用 `/转发复制` |

环检测按转发 ID 判定：同一条转发在自己的子树里再次出现时直接报错，不会死循环。

## 免责声明

本插件仅供合法的群聊演示、测试和娱乐用途。使用者应清楚标示模拟内容，并对使用方式及后果负责。

## 来源与许可

本项目是对 [SessionFaker](https://github.com/advent259141/astrbot_plugin_SessionFaker) 的重构，保留原作者署名，并继续使用 GNU AGPL-3.0 许可证。
