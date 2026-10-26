from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit


class ForwardMessageError(ValueError):
    """A user-facing input or policy error."""


@dataclass(frozen=True, slots=True)
class Limits:
    max_nodes: int = 20
    max_segments_per_node: int = 12
    max_total_text_length: int = 4000
    max_text_segment_length: int = 1000
    max_images: int = 8
    max_name_length: int = 64
    max_presentation_length: int = 100
    # 嵌套转发的边界：0 表示不限制深度，由环检测与总量预算兜底。
    max_forward_depth: int = 0
    max_total_nodes: int = 200
    max_forward_fetch: int = 32


@dataclass(frozen=True, slots=True)
class ForwardTree:
    """一段转发内容；既可内联节点，也可只带一个待拉取的转发 ID。"""

    nodes: tuple[ForwardNode, ...] = ()
    forward_id: str | None = None


@dataclass(frozen=True, slots=True)
class Segment:
    type: str
    value: str
    nested: ForwardTree | None = None


@dataclass(frozen=True, slots=True)
class ForwardNode:
    sender_id: str
    sender_name: str | None
    content: tuple[Segment, ...]


@dataclass(frozen=True, slots=True)
class Presentation:
    prompt: str = "[聊天记录]"
    summary: str = "查看 {count} 条转发消息"
    source: str = "聊天记录"


_INLINE_TOKEN = re.compile(
    r"(?<!\\)(?:\[(at|image|face|forward):([^\]\r\n]+)\]|"
    r"\[CQ:(image|face),([^\]\r\n]+)\])"
)
_SENDER_ID_TOKEN = r"(?:\d+|\[At:[^\]\r\n]+\])"
_TEXTUAL_AT_ID = re.compile(r"^\[At:([^\]\r\n]+)\]$")
_DSL_NODE = re.compile(
    rf"^\s*({_SENDER_ID_TOKEN})(?:\[([^\]\r\n]+)\])?\s*:\s*(.*)$"
)
_LEGACY_NODE = re.compile(rf"^\s*({_SENDER_ID_TOKEN})\s+(.+?)\s*$", re.DOTALL)

# OneBot 里表达"一层转发"的段类型名。node/nodes 是内联节点，
# forward/forward_msg 是以 ID 引用（或内联 content）的转发。
_FORWARD_TYPES = frozenset({"node", "nodes", "forward", "forward_msg", "multimsg"})


def _error(message: str) -> ForwardMessageError:
    return ForwardMessageError(message)


def _positive_id(value: Any, label: str) -> str:
    if isinstance(value, bool):
        raise _error(f"{label}必须是正整数")
    text = str(value).strip()
    if not text.isdigit() or int(text) <= 0 or len(text) > 20:
        raise _error(f"{label}必须是有效的数字 ID")
    return text


def _sender_id(value: Any, label: str) -> str:
    text = str(value).strip()
    match = _TEXTUAL_AT_ID.fullmatch(text)
    if match:
        text = match.group(1).strip()
    return _positive_id(text, label)


def _nonnegative_id(value: Any, label: str) -> str:
    if isinstance(value, bool):
        raise _error(f"{label}必须是非负整数")
    text = str(value).strip()
    if not text.isdigit() or len(text) > 20:
        raise _error(f"{label}必须是有效的非负整数")
    return text


def _name(value: Any, limits: Limits) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _error("显示名必须是字符串")
    value = value.strip()
    if not value:
        raise _error("显示名不能为空")
    if len(value) > limits.max_name_length:
        raise _error(f"显示名不能超过 {limits.max_name_length} 个字符")
    return value


def _image_source(value: Any, *, allow_local: bool = False) -> str:
    if not isinstance(value, str):
        raise _error("图片来源必须是字符串")
    value = value.strip()
    parsed = urlsplit(value)
    if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
        return value
    if allow_local and (
        PureWindowsPath(value).is_absolute() or PurePosixPath(value).is_absolute()
    ):
        return value
    raise _error("图片仅支持有效的 HTTP/HTTPS URL 或 AstrBot 临时文件绝对路径")


def _forward_id(value: Any) -> str:
    """转发 ID 既可能是短消息 ID，也可能是 QQ 的长 res_id。"""

    if isinstance(value, bool):
        raise _error("转发 ID 格式无效")
    text = str(value).strip()
    if not text or len(text) > 64:
        raise _error("转发 ID 格式无效")
    if not re.fullmatch(r"[0-9A-Za-z_\-+/=]+", text):
        raise _error("转发 ID 格式无效")
    return text


def _cq_unescape(value: str) -> str:
    return (
        value.replace("&#91;", "[")
        .replace("&#93;", "]")
        .replace("&#44;", ",")
        .replace("&amp;", "&")
    )


def _cq_params(value: str) -> dict[str, str]:
    params = {}
    for item in value.split(","):
        key, separator, raw_value = item.partition("=")
        if not separator or not key:
            raise _error("CQ 码参数格式错误")
        params[key] = _cq_unescape(raw_value)
    return params


def _segment_fields(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    """展开 OneBot 段里可能包了一层的 data 对象。"""

    data = raw.get("data")
    if not isinstance(data, Mapping):
        return raw
    merged = {key: value for key, value in raw.items() if key != "data"}
    merged.update(data)
    return merged


def _node_sender(fields: Mapping[str, Any]) -> tuple[str, str | None]:
    sender = fields.get("sender")
    sender_map = sender if isinstance(sender, Mapping) else {}
    raw_id = (
        fields.get("sender_id")
        or fields.get("user_id")
        or fields.get("uin")
        or sender_map.get("user_id")
    )
    if raw_id in (None, ""):
        raise _error("节点缺少发送者 QQ 号（sender_id/user_id）")
    sender_id = _sender_id(raw_id, "sender_id")
    raw_name = (
        fields.get("sender_name")
        or fields.get("nickname")
        or fields.get("name")
        or sender_map.get("nickname")
        or sender_map.get("card")
    )
    sender_name = None if raw_name in (None, "") else str(raw_name).strip() or None
    return sender_id, sender_name


def _segment_from_mapping(
    raw: Mapping[str, Any],
    limits: Limits,
) -> Segment:
    fields = _segment_fields(raw)
    segment_type = raw.get("type") or fields.get("type")
    if segment_type in _FORWARD_TYPES:
        return _forward_segment(fields, str(segment_type), limits)
    if segment_type == "text":
        value = fields.get("text")
        if not isinstance(value, str) or not value:
            raise _error("text 消息段必须包含非空 text")
        return Segment("text", value)
    if segment_type == "at":
        return Segment("at", _positive_id(fields.get("qq"), "@ 目标"))
    if segment_type == "image":
        return Segment("image", _image_source(fields.get("url") or fields.get("file")))
    if segment_type == "face":
        face_id = _nonnegative_id(fields.get("id"), "表情 ID")
        if int(face_id) > 65535:
            raise _error("表情 ID 不能大于 65535")
        return Segment("face", face_id)
    if segment_type == "reply":
        return Segment("reply", str(fields.get("id") or "").strip())
    if segment_type == "json":
        raw = fields.get("data")
        return Segment("json", raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False))
    if segment_type == "xml":
        return Segment("xml", str(fields.get("data") or ""))
    raise _error(f"不支持的消息段类型：{segment_type!r}")


def _forward_segment(
    fields: Mapping[str, Any],
    segment_type: str,
    limits: Limits,
) -> Segment:
    """把 node/nodes/forward 段折叠成统一的递归转发段。"""

    # 协议端若给出 res_id 就优先用它：数字短 id / msgId 在 NapCat 上常常解析不到内层。
    raw_id = (
        fields.get("res_id")
        or fields.get("forward_id")
        or fields.get("id")
        or fields.get("message_id")
    )

    if segment_type == "node":
        return Segment(
            "forward", "", ForwardTree(nodes=(_node_from_mapping(fields, limits),))
        )

    nested = (
        fields.get("content")
        or fields.get("message")
        or fields.get("messages")
        or fields.get("nodes")
        or fields.get("nodeList")
    )
    # parseMultMsg=true 时转发段会直接带内联节点，优先用它，省一次协议端往返。
    if (
        isinstance(nested, Sequence)
        and not isinstance(nested, (str, bytes))
        and nested
        and isinstance(nested[0], Mapping)
        and nested[0].get("type") in _FORWARD_TYPES
    ):
        nodes = tuple(_node_from_mapping(item, limits) for item in nested)
        return Segment(
            "forward",
            "",
            ForwardTree(
                nodes=nodes,
                forward_id=_forward_id(raw_id) if raw_id not in (None, "") else None,
            ),
        )

    if raw_id not in (None, ""):
        return Segment("forward", "", ForwardTree(forward_id=_forward_id(raw_id)))
    raise _error("转发段缺少 id 或节点内容")


def _append_text(segments: list[Segment], text: str) -> None:
    text = text.replace(r"\[", "[")
    if not text:
        return
    if segments and segments[-1].type == "text":
        segments[-1] = Segment("text", segments[-1].value + text)
    else:
        segments.append(Segment("text", text))


def parse_inline_content(text: str) -> tuple[Segment, ...]:
    if not isinstance(text, str):
        raise _error("节点内容必须是字符串")
    segments: list[Segment] = []
    cursor = 0
    for match in _INLINE_TOKEN.finditer(text):
        _append_text(segments, text[cursor : match.start()])
        token_type, token_value, cq_type, cq_value = match.groups()
        is_cq_code = cq_type is not None
        if is_cq_code:
            token_type = cq_type
            params = _cq_params(cq_value)
            token_value = params.get("file") if token_type == "image" else params.get("id")
            if token_value is None:
                raise _error(f"CQ {token_type} 码缺少必要参数")
        if token_type == "at":
            segments.append(Segment("at", _positive_id(token_value, "@ 目标")))
        elif token_type == "image":
            segments.append(
                Segment(
                    "image",
                    _image_source(token_value, allow_local=is_cq_code),
                )
            )
        elif token_type == "forward":
            segments.append(
                Segment("forward", "", ForwardTree(forward_id=_forward_id(token_value)))
            )
        else:
            face_id = _nonnegative_id(token_value, "表情 ID")
            if int(face_id) > 65535:
                raise _error("表情 ID 不能大于 65535")
            segments.append(Segment("face", face_id))
        cursor = match.end()
    _append_text(segments, text[cursor:])
    if not segments:
        raise _error("节点内容不能为空")
    return tuple(segments)


def _content(raw: Any, limits: Limits) -> tuple[Segment, ...]:
    if isinstance(raw, str):
        if not raw:
            raise _error("节点内容不能为空")
        return parse_inline_content(raw)
    if not isinstance(raw, list) or not raw:
        raise _error("content 必须是非空字符串或消息段数组")
    segments: list[Segment] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise _error("消息段必须是对象")
        segments.append(_segment_from_mapping(item, limits))
    return tuple(segments)


def _node_from_mapping(raw: Mapping[str, Any], limits: Limits) -> ForwardNode:
    fields = _segment_fields(raw)
    sender_id, sender_name = _node_sender(fields)
    raw_content = (
        fields.get("content")
        or fields.get("message")
        or fields.get("message_chain")
    )
    if raw_content in (None, "", [], {}):
        raise _error("节点缺少 content")
    sender_name = _name(sender_name, limits)
    return ForwardNode(
        sender_id=sender_id,
        sender_name=sender_name,
        content=_content(raw_content, limits),
    )


def parse_json_nodes(
    value: str | Sequence[Mapping[str, Any]],
    limits: Limits | None = None,
) -> list[ForwardNode]:
    limits = limits or Limits()
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise _error(f"JSON 格式错误：第 {exc.lineno} 行第 {exc.colno} 列") from exc
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _error("JSON 顶层必须是节点数组")
    nodes = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise _error("每个节点必须是对象")
        nodes.append(_node_from_mapping(raw, limits))
    return validate_nodes(nodes, limits)


def parse_dsl(value: str, limits: Limits | None = None) -> list[ForwardNode]:
    limits = limits or Limits()
    nodes: list[ForwardNode] = []
    for line_number, line in enumerate(value.splitlines(), 1):
        if not line.strip():
            continue
        match = _DSL_NODE.fullmatch(line)
        if not match:
            raise _error(f"第 {line_number} 行格式错误，应为 QQ号[显示名]: 内容")
        sender_id, sender_name, content = match.groups()
        try:
            node = ForwardNode(
                sender_id=_sender_id(sender_id, "QQ 号"),
                sender_name=_name(sender_name, limits),
                content=parse_inline_content(content),
            )
        except ForwardMessageError as exc:
            raise _error(f"第 {line_number} 行：{exc}") from exc
        nodes.append(node)
    return validate_nodes(nodes, limits)


def parse_legacy(
    value: str,
    limits: Limits | None = None,
    images_by_node: Sequence[Sequence[str]] | None = None,
) -> list[ForwardNode]:
    limits = limits or Limits()
    parts = [part.strip() for part in value.split("|") if part.strip()]
    nodes: list[ForwardNode] = []
    for index, part in enumerate(parts):
        match = _LEGACY_NODE.fullmatch(part)
        if not match:
            raise _error(
                f"第 {index + 1} 段格式错误，应为 QQ号/[At:QQ号] 内容"
            )
        sender_id, text = match.groups()
        segments = [Segment("text", text)]
        if images_by_node and index < len(images_by_node):
            segments.extend(
                Segment("image", _image_source(url))
                for url in images_by_node[index]
            )
        nodes.append(ForwardNode(_sender_id(sender_id, "QQ 号"), None, tuple(segments)))
    return validate_nodes(nodes, limits)


def parse_command_input(value: str, limits: Limits | None = None) -> list[ForwardNode]:
    stripped = value.strip()
    if not stripped:
        raise _error("未提供转发节点")
    if stripped.startswith("["):
        return parse_json_nodes(stripped, limits)
    return parse_dsl(stripped, limits)


def parse_forward_payload(
    payload: Any,
    limits: Limits | None = None,
) -> tuple[ForwardNode, ...]:
    """把 get_forward_msg 的原始应答解析成节点树（保留嵌套结构）。"""

    limits = limits or Limits()
    nodes_raw: Any = None
    shape = ""
    if isinstance(payload, Mapping):
        data = payload.get("data")
        container = data if isinstance(data, Mapping) else payload
        for key in ("messages", "message", "nodes", "nodeList", "content"):
            candidate = container.get(key)
            if isinstance(candidate, Sequence) and not isinstance(
                candidate, (str, bytes)
            ):
                nodes_raw = candidate
                break
        if nodes_raw is None:
            shape = f"keys={sorted(container)[:6]}" if isinstance(container, Mapping) else ""
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        nodes_raw = payload
    if not nodes_raw:
        raise _error("转发内容为空或格式不受支持" + (f"（{shape}）" if shape else ""))
    nodes: list[ForwardNode] = []
    skipped = 0
    for raw in nodes_raw:
        if not isinstance(raw, Mapping):
            continue
        try:
            nodes.append(_node_from_mapping(raw, limits))
        except ForwardMessageError:
            # 占位/空节点跳过，不让一个坏节点毁掉整层内容。
            skipped += 1
            continue
    if not nodes:
        raise _error(
            "转发内容为空或格式不受支持"
            + (f"（{shape}）" if shape else "")
            + (f"（跳过 {skipped} 个空节点）" if skipped else "")
        )
    return _fill_missing_sender_names(tuple(nodes))


def _fill_missing_sender_names(
    nodes: tuple[ForwardNode, ...],
) -> tuple[ForwardNode, ...]:
    filled: list[ForwardNode] = []
    for node in nodes:
        content: list[Segment] = []
        for segment in node.content:
            if segment.type == "forward" and segment.nested is not None:
                content.append(
                    Segment(
                        "forward",
                        "",
                        ForwardTree(
                            nodes=_fill_missing_sender_names(segment.nested.nodes),
                            forward_id=segment.nested.forward_id,
                        ),
                    )
                )
            else:
                content.append(segment)
        name = node.sender_name or f"QQ用户 {node.sender_id}"
        filled.append(replace(node, sender_name=name, content=tuple(content)))
    return tuple(filled)


def iter_nodes(nodes: Sequence[ForwardNode], *, depth: int = 0):
    """深度优先遍历整棵转发树。"""

    for node in nodes:
        yield node, depth
        for segment in node.content:
            if segment.type != "forward" or segment.nested is None:
                continue
            yield from iter_nodes(segment.nested.nodes, depth=depth + 1)


def validate_nodes(
    nodes: Sequence[ForwardNode],
    limits: Limits | None = None,
) -> list[ForwardNode]:
    limits = limits or Limits()
    if not nodes:
        raise _error("至少需要一个转发节点")
    if len(nodes) > limits.max_nodes:
        raise _error(f"节点数量不能超过 {limits.max_nodes}")

    total_text = 0
    total_images = 0
    total_nodes = 0

    def check_level(
        level: Sequence[ForwardNode],
        depth: int,
        path: tuple[str, ...],
    ) -> None:
        nonlocal total_text, total_images, total_nodes
        if limits.max_forward_depth and depth > limits.max_forward_depth:
            raise _error(f"转发嵌套深度不能超过 {limits.max_forward_depth} 层")
        for node_index, node in enumerate(level, 1):
            total_nodes += 1
            if total_nodes > limits.max_total_nodes:
                raise _error(f"转发节点总数不能超过 {limits.max_total_nodes} 个")
            _positive_id(node.sender_id, f"第 {node_index} 个节点的 sender_id")
            if node.sender_name is not None:
                _name(node.sender_name, limits)
            if not node.content:
                raise _error(f"第 {node_index} 个节点内容为空")
            if len(node.content) > limits.max_segments_per_node:
                raise _error(
                    f"第 {node_index} 个节点消息段不能超过 "
                    f"{limits.max_segments_per_node} 个"
                )
            for segment in node.content:
                if segment.type == "text":
                    if len(segment.value) > limits.max_text_segment_length:
                        raise _error(
                            f"第 {node_index} 个节点的单段文本不能超过 "
                            f"{limits.max_text_segment_length} 个字符"
                        )
                    total_text += len(segment.value)
                elif segment.type == "image":
                    _image_source(segment.value, allow_local=True)
                    total_images += 1
                elif segment.type == "at":
                    _positive_id(segment.value, "@ 目标")
                elif segment.type == "face":
                    face_id = _nonnegative_id(segment.value, "表情 ID")
                    if int(face_id) > 65535:
                        raise _error("表情 ID 不能大于 65535")
                elif segment.type in {"reply", "json", "xml"}:
                    continue
                elif segment.type == "forward":
                    tree = segment.nested
                    if tree is None:
                        raise _error("嵌套转发缺少内容")
                    if tree.forward_id and tree.forward_id in path:
                        raise _error(f"检测到循环转发：{tree.forward_id}")
                    if tree.nodes:
                        pure_forward = len(node.content) == 1
                        if not pure_forward and not tree.forward_id:
                            raise _error(
                                f"第 {node_index} 个节点的嵌套转发必须独占节点内容；"
                                "与普通消息混排时需要用 forward 段携带转发 ID"
                            )
                        if pure_forward:
                            next_path = (
                                (*path, tree.forward_id) if tree.forward_id else path
                            )
                            check_level(tree.nodes, depth + 1, next_path)
                else:
                    raise _error(f"不支持的消息段类型：{segment.type!r}")

    check_level(nodes, 0, ())
    if total_text > limits.max_total_text_length:
        raise _error(f"总文本长度不能超过 {limits.max_total_text_length} 个字符")
    if total_images > limits.max_images:
        raise _error(f"图片数量不能超过 {limits.max_images} 张")
    return list(nodes)


def referenced_member_ids(nodes: Iterable[ForwardNode]) -> set[str]:
    """只统计顶层节点的成员引用；嵌套转发里的发送者来自别的会话，不参与群成员校验。"""

    member_ids = {node.sender_id for node in nodes}
    member_ids.update(
        segment.value
        for node in nodes
        for segment in node.content
        if segment.type == "at"
    )
    return member_ids


def forward_tree_ids(nodes: Sequence[ForwardNode]) -> set[str]:
    """收集整棵树里所有只有 ID、尚未展开的转发 ID。"""

    found: set[str] = set()
    for node, _depth in iter_nodes(nodes):
        for segment in node.content:
            if segment.type != "forward" or segment.nested is None:
                continue
            tree = segment.nested
            if tree.forward_id and not tree.nodes:
                found.add(tree.forward_id)
    return found


def substitute_forward_trees(
    nodes: Sequence[ForwardNode],
    resolved: Mapping[str, tuple[ForwardNode, ...]],
) -> tuple[ForwardNode, ...]:
    """把已拉取的转发内容填回惰性节点；未命中的保持原样。"""

    result: list[ForwardNode] = []
    for node in nodes:
        content: list[Segment] = []
        for segment in node.content:
            if segment.type != "forward" or segment.nested is None:
                content.append(segment)
                continue
            tree = segment.nested
            if tree.forward_id and tree.forward_id in resolved:
                inner = substitute_forward_trees(resolved[tree.forward_id], resolved)
                content.append(
                    Segment(
                        "forward",
                        "",
                        ForwardTree(nodes=inner, forward_id=tree.forward_id),
                    )
                )
                continue
            inner_nodes = substitute_forward_trees(tree.nodes, resolved)
            content.append(
                Segment(
                    "forward",
                    "",
                    ForwardTree(nodes=inner_nodes, forward_id=tree.forward_id),
                )
            )
        result.append(replace(node, content=tuple(content)))
    return tuple(result)


def resolve_member_names(
    nodes: Sequence[ForwardNode],
    member_names: Mapping[str, str],
) -> list[ForwardNode]:
    missing = sorted(referenced_member_ids(nodes) - member_names.keys(), key=int)
    if missing:
        raise _error(f"以下 QQ 号不是当前群成员：{', '.join(missing)}")
    return [
        replace(node, sender_name=node.sender_name or member_names[node.sender_id])
        for node in nodes
    ]


def _segment_payload(segment: Segment) -> dict[str, Any]:
    if segment.type == "text":
        return {"type": "text", "data": {"text": segment.value}}
    if segment.type == "at":
        return {"type": "at", "data": {"qq": segment.value}}
    if segment.type == "image":
        return {"type": "image", "data": {"file": segment.value}}
    if segment.type == "face":
        return {"type": "face", "data": {"id": int(segment.value)}}
    if segment.type == "reply":
        return {"type": "reply", "data": {"id": segment.value}}
    if segment.type == "json":
        return {"type": "json", "data": {"data": segment.value}}
    if segment.type == "xml":
        return {"type": "xml", "data": {"data": segment.value}}
    raise _error(f"不支持的消息段类型：{segment.type!r}")


def _preview(node: ForwardNode, max_length: int = 80) -> str:
    chunks = []
    for segment in node.content:
        if segment.type == "text":
            chunks.append(segment.value.replace("\n", " "))
        elif segment.type == "at":
            chunks.append(f"@{segment.value}")
        elif segment.type == "image":
            chunks.append("[图片]")
        elif segment.type == "face":
            chunks.append("[表情]")
        elif segment.type == "forward":
            chunks.append("[转发消息]")
        elif segment.type == "reply":
            chunks.append("[回复]")
        elif segment.type in {"json", "xml"}:
            chunks.append("[卡片]")
    value = "".join(chunks).strip() or "[消息]"
    return value if len(value) <= max_length else value[: max_length - 1] + "…"


def _node_payload(node: ForwardNode) -> dict[str, Any]:
    if not node.sender_name:
        raise _error(f"QQ {node.sender_id} 尚未解析显示名")
    content: list[dict[str, Any]] = []
    pure_forward = len(node.content) == 1 and node.content[0].type == "forward"
    for segment in node.content:
        if segment.type != "forward":
            content.append(_segment_payload(segment))
            continue
        tree = segment.nested
        if tree is None or not tree.nodes:
            raise _error("嵌套转发尚未展开，请先拉取转发内容")
        if pure_forward:
            content.extend(_node_payload(item) for item in tree.nodes)
            continue
        if not tree.forward_id:
            raise _error("与普通消息混排的转发缺少 ID，QQ 无法表达该结构")
        content.append({"type": "forward", "data": {"id": tree.forward_id}})
    return {
        "type": "node",
        "data": {
            "user_id": node.sender_id,
            "nickname": node.sender_name,
            "content": content,
        },
    }


def build_forward_payload(
    group_id: str | int,
    nodes: Sequence[ForwardNode],
    presentation: Presentation | None = None,
    limits: Limits | None = None,
) -> dict[str, Any]:
    limits = limits or Limits()
    nodes = validate_nodes(nodes, limits)
    group_id_text = _positive_id(group_id, "群号")
    presentation = presentation or Presentation()
    prompt = _presentation_text(presentation.prompt, "prompt", limits)
    source = _presentation_text(presentation.source, "source", limits)
    try:
        summary_value = presentation.summary.format(count=len(nodes))
    except (KeyError, ValueError) as exc:
        raise _error("summary 模板仅支持 {count} 占位符") from exc
    summary = _presentation_text(summary_value, "summary", limits)
    messages = [_node_payload(node) for node in nodes]
    return {
        "group_id": int(group_id_text),
        "messages": messages,
        "prompt": prompt,
        "summary": summary,
        "source": source,
        "news": [
            {"text": f"{node.sender_name}: {_preview(node)}"}
            for node in nodes[:4]
        ],
    }


def forward_nodes_to_text(
    nodes: Sequence[ForwardNode],
    *,
    indent: str = "",
    max_lines: int = 200,
) -> str:
    """把节点树渲染成缩进文本，供命令与 LLM 读取使用。"""

    lines: list[str] = []

    def render(level: Sequence[ForwardNode], prefix: str) -> None:
        for node in level:
            if len(lines) >= max_lines:
                return
            name = node.sender_name or f"QQ用户 {node.sender_id}"
            lines.append(f"{prefix}{name}({node.sender_id}): {_preview(node)}")
            for segment in node.content:
                if segment.type != "forward" or segment.nested is None:
                    continue
                tree = segment.nested
                if tree.forward_id and not tree.nodes:
                    if len(lines) < max_lines:
                        lines.append(f"{prefix}  └─[转发 {tree.forward_id} 未展开]")
                    continue
                render(tree.nodes, prefix + "  ")

    render(nodes, indent)
    if len(lines) >= max_lines:
        lines.append("…（已达显示上限）")
    return "\n".join(lines)


def forward_nodes_to_struct(
    nodes: Sequence[ForwardNode],
    *,
    max_nodes: int = 400,
) -> dict[str, Any]:
    """把节点树转成结构化字典：保留每层嵌套关系，未展开的层显式标记。"""

    stats = {"nodes": 0, "depth": 0, "unresolved": []}
    dropped = 0

    def convert_node(node: ForwardNode, level: int) -> dict[str, Any]:
        if stats["nodes"] >= max_nodes:
            return {}
        stats["nodes"] += 1
        stats["depth"] = max(stats["depth"], level)
        segments: list[dict[str, Any]] = []
        for segment in node.content:
            if segment.type == "forward":
                tree = segment.nested
                item: dict[str, Any] = {
                    "type": "forward",
                    "expanded": bool(tree and tree.nodes),
                    "depth": level + 1,
                }
                if tree and tree.forward_id:
                    item["referenced_id"] = tree.forward_id
                if tree and tree.nodes:
                    item["nodes"] = [convert_node(child, level + 1) for child in tree.nodes]
                elif tree and tree.forward_id:
                    stats["unresolved"].append(tree.forward_id)
                segments.append(item)
                continue
            segments.append({"type": segment.type, "value": segment.value})
        return {
            "sender_id": node.sender_id,
            "sender_name": node.sender_name or f"QQ用户 {node.sender_id}",
            "depth": level,
            "segments": segments,
        }

    tree = []
    for node in nodes:
        if stats["nodes"] >= max_nodes:
            dropped += 1
            continue
        tree.append(convert_node(node, 0))
    result: dict[str, Any] = {
        "type": "forward_message",
        "node_count": stats["nodes"],
        "max_depth": stats["depth"],
        "fully_expanded": not stats["unresolved"],
        "unresolved_ids": stats["unresolved"][:20],
        "nodes": tree,
    }
    if dropped:
        result["truncated"] = True
    return result


def _presentation_text(value: str, label: str, limits: Limits) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error(f"{label}不能为空")
    value = value.strip()
    if len(value) > limits.max_presentation_length:
        raise _error(f"{label}不能超过 {limits.max_presentation_length} 个字符")
    return value
