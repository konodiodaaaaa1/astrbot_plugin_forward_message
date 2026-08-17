# QQ 合并转发助手

QQ 合并转发助手是面向 AstrBot 4.26.8+ 和 AIOCQHTTP/NapCat 的 QQ 合并转发插件。
它支持命令和受控 LLM Tool，不使用外部 QQ 昵称 API，也不接受目标群号。

## 安全边界

- 仅支持群聊，私聊直接拒绝。
- 发送目标始终是触发命令或 Tool 的当前群。
- 所有伪造发送者和 `at` 目标必须是当前群成员。
- 昵称来自当前群名片，其次是 QQ 昵称；显式显示名仅作为展示覆盖。
- 可配置群白名单和 AstrBot/QQ群管理员权限。
- 节点、消息段、文本和图片均有可配置上限。
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

## 旧命令

默认保留兼容入口，可在配置中关闭：

```text
/伪造消息 123456 第一条消息 | 654321 第二条消息
```

旧命令的发送者 QQ 号同样可以使用 `[At:QQ号]`，例如 `/伪造消息 [At:123456] 第一条消息`。

旧命令中的原消息图片按出现位置分配给对应节点。

## LLM Tool

插件注册 `send_qq_forward_message`。参数 `nodes_json` 使用与新命令相同的 JSON 节点数组；`prompt`、`summary` 和 `source` 可选。Tool 不提供群号参数，因此模型不能跨会话路由消息。

## 配置

`permission_mode` 默认为 `everyone`，还支持：

- `astrbot_admin`
- `qq_group_admin`
- `admin_only`（满足任一种管理员身份）

`group_whitelist` 留空表示不限制群，多个群号用逗号或空格分隔。

## 免责声明

本插件仅供合法的群聊演示、测试和娱乐用途。使用者应清楚标示模拟内容，并对使用方式及后果负责。

## 来源与许可

本项目是对 [SessionFaker](https://github.com/advent259141/astrbot_plugin_SessionFaker) 的重构，保留原作者署名，并继续使用 GNU AGPL-3.0 许可证。
