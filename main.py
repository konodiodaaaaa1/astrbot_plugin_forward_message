from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Face, Forward, Image, Json, Node, Nodes, Plain, Reply
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

try:  # older/newer AstrBot may expose the data root under a different name
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path as _get_data_path
except Exception:  # noqa: BLE001
    _get_data_path = None

from .forward_message import (
    ForwardMessageError,
    ForwardNode,
    Limits,
    Presentation,
    build_forward_payload,
    forward_nodes_to_struct,
    forward_nodes_to_text,
    forward_tree_ids,
    parse_command_input,
    parse_forward_payload,
    parse_json_nodes,
    parse_legacy,
    referenced_member_ids,
    resolve_member_names,
    substitute_forward_trees,
)


PLUGIN_NAME = "QQ Forward Message"
SUPPORTED_PLATFORM = "aiocqhttp"
_COMMAND_PREFIX = re.compile(r"^\s*/?(?:伪造转发|伪造消息)\s*", re.IGNORECASE)
_COMMAND_AT_PLACEHOLDER = re.compile(r"\[At:([^\]\r\n]+)\]")
_READ_ARGUMENT_PREFIX = re.compile(r"^\s*/?(?:查看转发|转发复制|转发调试)\s*", re.IGNORECASE)


class SessionFakerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config if config is not None else {}

    # ---- 本地转发缓存：自己发出去的合并转发，NapCat 之后可能取不回内容 ----

    def _cache_path(self) -> Path:
        root = None
        if _get_data_path is not None:
            try:
                root = Path(_get_data_path())
            except Exception:  # noqa: BLE001
                root = None
        if root is None:
            root = Path(get_astrbot_temp_path()).parent
        return root / "plugin_data" / "astrbot_plugin_forward_message" / "forward_cache.json"

    def _cache_load(self) -> dict[str, Any]:
        path = self._cache_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _cache_put(self, message_id: str, messages: list[Any]) -> None:
        key = str(message_id or "").strip()
        if not key.isdigit() or not isinstance(messages, list) or not messages:
            return
        data = self._cache_load()
        data[key] = {"ts": int(__import__("time").time()), "messages": messages}
        if len(data) > 200:
            for old in sorted(data, key=lambda item: data[item].get("ts", 0))[: len(data) - 200]:
                data.pop(old, None)
        path = self._cache_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:  # noqa: BLE001
            logger.warning("转发缓存写入失败：%s", exc)

    def _cache_get(self, message_id: str) -> Any | None:
        entry = self._cache_load().get(str(message_id or "").strip())
        if not isinstance(entry, dict):
            return None
        messages = entry.get("messages")
        return {"messages": messages} if isinstance(messages, list) and messages else None

    def _config_int(self, key: str, default: int) -> int:
        try:
            return max(1, int(self.config.get(key, default)))
        except (TypeError, ValueError):
            return default

    def _config_nonnegative_int(self, key: str, default: int) -> int:
        try:
            return max(0, int(self.config.get(key, default)))
        except (TypeError, ValueError):
            return default

    def _limits(self) -> Limits:
        return Limits(
            max_nodes=self._config_int("max_nodes", 20),
            max_segments_per_node=self._config_int("max_segments_per_node", 12),
            max_total_text_length=self._config_int("max_total_text_length", 4000),
            max_text_segment_length=self._config_int("max_text_segment_length", 1000),
            max_images=self._config_int("max_images", 8),
            max_name_length=self._config_int("max_name_length", 64),
            max_presentation_length=self._config_int("max_presentation_length", 100),
            max_forward_depth=self._config_nonnegative_int("max_forward_depth", 0),
            max_total_nodes=self._config_int("max_total_nodes", 200),
            max_forward_fetch=self._config_int("max_forward_fetch", 32),
        )

    def _presentation(
        self,
        prompt: str = "",
        summary: str = "",
        source: str = "",
    ) -> Presentation:
        return Presentation(
            prompt=prompt or str(self.config.get("forward_prompt", "[聊天记录]")),
            summary=summary
            or str(self.config.get("forward_summary", "查看 {count} 条转发消息")),
            source=source or str(self.config.get("forward_source", "聊天记录")),
        )

    def _group_whitelist(self) -> set[str]:
        raw = self.config.get("group_whitelist", "")
        if isinstance(raw, Sequence) and not isinstance(raw, str):
            values = raw
        else:
            values = re.split(r"[,，\s]+", str(raw))
        return {str(value).strip() for value in values if str(value).strip()}

    @staticmethod
    def _routing_params(event: AstrMessageEvent) -> dict[str, Any]:
        self_id = str(event.get_self_id() or "").strip()
        return {"self_id": int(self_id)} if self_id.isdigit() else {}

    @staticmethod
    def _require_supported_platform(event: AstrMessageEvent) -> None:
        if str(event.get_platform_name()).lower() != SUPPORTED_PLATFORM:
            raise ForwardMessageError("仅支持 AIOCQHTTP/NapCat 平台")

    @staticmethod
    def _peer(event: AstrMessageEvent) -> tuple[str, str]:
        """不再区分群聊/私聊：群聊取群号，私聊取对方 QQ 号，两者都当会话目标。"""

        if str(event.get_platform_name()).lower() != SUPPORTED_PLATFORM:
            raise ForwardMessageError("仅支持 AIOCQHTTP/NapCat 平台")
        group_id = str(event.get_group_id() or "").strip()
        if group_id.isdigit() and int(group_id) > 0:
            return "group", group_id
        peer_id = str(event.get_sender_id() or "").strip()
        if peer_id.isdigit() and int(peer_id) > 0:
            return "private", peer_id
        raise ForwardMessageError("无法识别当前会话目标")

    async def _user_info(
        self,
        event: AstrMessageEvent,
        peer_kind: str,
        peer_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """取用户资料，群聊走群成员、私聊走陌生人，取不到就返回空字典而不是报错。"""

        routing = self._routing_params(event)
        actions: list[tuple[str, dict[str, Any]]] = []
        if peer_kind == "group":
            actions.append(
                (
                    "get_group_member_info",
                    {"group_id": int(peer_id), "user_id": int(user_id), "no_cache": False},
                )
            )
        actions.append(("get_stranger_info", {"user_id": int(user_id), "no_cache": False}))
        for action, params in actions:
            try:
                result = await event.bot.call_action(action, **params, **routing)
            except Exception:  # noqa: BLE001 - 资料缺失不应阻断发送
                continue
            if isinstance(result, dict) and result:
                return result
        return {}

    async def _check_access(
        self,
        event: AstrMessageEvent,
        peer_kind: str,
        peer_id: str,
    ) -> dict[str, Any]:
        whitelist = self._group_whitelist()
        if whitelist and peer_kind == "group" and peer_id not in whitelist:
            raise ForwardMessageError("当前群不在插件白名单中")

        sender_id = str(event.get_sender_id() or "").strip()
        if not sender_id.isdigit():
            raise ForwardMessageError("无法识别当前发送者")
        sender_info = await self._user_info(event, peer_kind, peer_id, sender_id)
        mode = str(self.config.get("permission_mode", "everyone")).strip().lower()
        if mode not in {"everyone", "astrbot_admin", "qq_group_admin", "admin_only"}:
            raise ForwardMessageError(f"无效的权限模式：{mode}")

        astrbot_admin = bool(event.is_admin())
        qq_admin = str(sender_info.get("role", "member")).lower() in {"admin", "owner"}
        allowed = {
            "everyone": True,
            "astrbot_admin": astrbot_admin,
            "qq_group_admin": qq_admin,
            "admin_only": astrbot_admin or qq_admin,
        }[mode]
        if not allowed:
            raise ForwardMessageError("你没有使用该功能的权限")
        return sender_info

    @staticmethod
    def _member_name(info: dict[str, Any], user_id: str) -> str:
        return str(info.get("card") or info.get("nickname") or f"QQ用户 {user_id}")

    async def _resolve_names(
        self,
        event: AstrMessageEvent,
        peer_kind: str,
        peer_id: str,
        nodes: list[ForwardNode],
        sender_info: dict[str, Any],
    ) -> list[ForwardNode]:
        """尽力解析显示名；取不到就退回 QQ号/占位名，不再要求必须是群成员。"""

        names: dict[str, str] = {}
        event_sender = str(event.get_sender_id() or "").strip()
        if event_sender.isdigit():
            names[event_sender] = self._member_name(sender_info, event_sender)
        for user_id in sorted(referenced_member_ids(nodes) - names.keys(), key=int):
            info = await self._user_info(event, peer_kind, peer_id, user_id)
            names[user_id] = self._member_name(info, user_id)
        return resolve_member_names(nodes, names)

    @classmethod
    def _usable_forward_payload(cls, payload: Any) -> bool:
        """应答里必须至少有一个"真有消息段"的节点，空壳节点要继续换策略。"""

        if not isinstance(payload, dict) or not payload:
            return False
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        for key in ("messages", "message", "nodes", "nodeList", "content"):
            value = data.get(key) if isinstance(data, dict) else None
            if not isinstance(value, list) or not value:
                continue
            for node in value:
                if not isinstance(node, dict):
                    continue
                node_data = node.get("data") if isinstance(node.get("data"), dict) else node
                for field in ("content", "message", "messages"):
                    segments = node_data.get(field) if isinstance(node_data, dict) else None
                    if isinstance(segments, list) and segments:
                        return True
        return False

    @classmethod
    def _payload_fingerprint(cls, payload: Any) -> str:
        """给日志用的形状指纹：一眼区分"空壳节点"和"真空应答"。"""

        if not isinstance(payload, dict):
            return type(payload).__name__
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        keys = sorted(data)[:6] if isinstance(data, dict) else []
        for key in ("messages", "message", "nodes", "nodeList", "content"):
            value = data.get(key) if isinstance(data, dict) else None
            if isinstance(value, list) and value:
                first = value[0]
                node_keys = (
                    sorted(first)[:6]
                    if isinstance(first, dict)
                    else type(first).__name__
                )
                return f"keys={keys},len={len(value)},first={node_keys}"
        return f"keys={keys}"

    @staticmethod
    def _message_chain(message: Any) -> list[Any]:
        if not isinstance(message, dict):
            return []
        data = message.get("data") if isinstance(message.get("data"), dict) else message
        for key in ("message", "content", "messages"):
            chain = data.get(key)
            if isinstance(chain, list):
                return chain
        return []

    @classmethod
    def _forward_ids_in_chain(cls, chain: list[Any]) -> list[str]:
        ids: list[str] = []
        for segment in chain:
            if not isinstance(segment, dict):
                continue
            data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
            segment_type = segment.get("type")
            if segment_type in {"forward", "forward_msg", "node", "nodes"}:
                value = data.get("res_id") or data.get("id") or data.get("message_id")
                if value not in (None, "") and str(value) not in ids:
                    ids.append(str(value))
            elif segment_type == "json":
                raw = data.get("data")
                if isinstance(raw, str):
                    try:
                        parsed = json.loads(raw.replace("&#44;", ","))
                    except Exception:
                        continue
                    resid = (
                        parsed.get("meta", {})
                        .get("detail", {})
                        .get("resid")
                    ) if isinstance(parsed, dict) else None
                    if resid and str(resid) not in ids:
                        ids.append(str(resid))
        return ids

    @classmethod
    def _inline_forward_content(cls, chain: list[Any]) -> dict[str, Any] | None:
        for segment in chain:
            if not isinstance(segment, dict):
                continue
            data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
            if segment.get("type") not in {"forward", "forward_msg", "nodes", "node"}:
                continue
            content = data.get("content") or data.get("message")
            if isinstance(content, list) and content:
                return {"messages": content}
        return None

    async def _forward_payload_via_get_msg(
        self,
        event: AstrMessageEvent,
        forward_id: str,
        reasons: list[str],
    ) -> Any | None:
        """先 get_msg 拿到那条消息，再从中提取转发 ID 或内联内容。"""

        routing = self._routing_params(event)
        message = None
        try:
            # get_msg 只接受 message_id，传 id 会得到 1400 schema 错误，别再试。
            message = await event.bot.call_action(
                "get_msg", message_id=forward_id, **routing
            )
        except Exception as exc:  # noqa: BLE001
            reasons.append(f"get_msg={type(exc).__name__}:{exc}")
            message = None
        if not isinstance(message, dict) or not message:
            return None
        chain = self._message_chain(message)
        inline = self._inline_forward_content(chain)
        if inline is not None:
            return inline
        for resid in self._json_resids_in_chain(chain):
            try:
                result = await event.bot.call_action(
                    "get_forward_msg", id=resid, **routing
                )
            except Exception as exc:  # noqa: BLE001
                reasons.append(f"json_resid({resid})={type(exc).__name__}:{exc}")
                continue
            if self._usable_forward_payload(result):
                return result
            reasons.append(
                f"json_resid({resid})=空壳[{self._payload_fingerprint(result)}]"
            )
        for nested_id in self._forward_ids_in_chain(chain):
            for attempt in ("message_id", "id"):
                try:
                    result = await event.bot.call_action(
                        "get_forward_msg", **{attempt: nested_id}, **routing
                    )
                except Exception as exc:  # noqa: BLE001
                    reasons.append(f"nested({nested_id},{attempt})={type(exc).__name__}:{exc}")
                    continue
                if self._usable_forward_payload(result):
                    return result
                reasons.append(
                    f"nested({nested_id},{attempt})=空壳[{self._payload_fingerprint(result)}]"
                )
        return None

    async def _fetch_forward_payload(
        self,
        event: AstrMessageEvent,
        forward_id: str,
    ) -> Any:
        """按 msgId / res_id 拉取一层转发内容，失败时把每次尝试的原因带出来。"""

        routing = self._routing_params(event)
        reasons: list[str] = []
        for key in ("message_id", "id"):
            try:
                result = await event.bot.call_action(
                    "get_forward_msg",
                    **{key: forward_id},
                    **routing,
                )
            except Exception as exc:  # noqa: BLE001 - 协议端会因入参形态不同而抛错
                reasons.append(f"{key}={type(exc).__name__}:{exc}")
                continue
            if self._usable_forward_payload(result):
                return result
            reasons.append(f"{key}=空壳[{self._payload_fingerprint(result)}]")
        via_message = await self._forward_payload_via_get_msg(event, forward_id, reasons)
        if via_message is not None:
            return via_message
        own_message_id = str(
            getattr(getattr(event, "message_obj", None), "message_id", "") or ""
        ).strip()
        if own_message_id.isdigit() and own_message_id != str(forward_id):
            via_own = await self._forward_payload_via_get_msg(
                event, own_message_id, reasons
            )
            if via_own is not None:
                return via_own
        group_id = str(event.get_group_id() or "").strip()
        warm_action: dict[str, Any] | None = None
        if group_id.isdigit():
            warm_action = {"get_group_msg_history": {"group_id": int(group_id)}}
        else:
            peer_id = str(getattr(event, "get_sender_id", lambda: "")() or "").strip()
            if peer_id.isdigit():
                warm_action = {"get_friend_msg_history": {"user_id": int(peer_id)}}
        if warm_action:
            warm_name, warm_params = next(iter(warm_action.items()))
            try:
                await event.bot.call_action(
                    warm_name, count=50, **warm_params, **routing
                )
            except Exception as exc:  # noqa: BLE001
                reasons.append(f"warm={type(exc).__name__}:{exc}")
            for key in ("message_id", "id"):
                try:
                    result = await event.bot.call_action(
                        "get_forward_msg",
                        **{key: forward_id},
                        **routing,
                    )
                except Exception as exc:  # noqa: BLE001
                    reasons.append(f"warm-{key}={type(exc).__name__}:{exc}")
                    continue
                if self._usable_forward_payload(result):
                    return result
                reasons.append(f"warm-{key}=空壳[{self._payload_fingerprint(result)}]")
        via_history = await self._forward_payload_via_history(
            event, forward_id, reasons
        )
        if via_history is not None:
            return via_history
        logger.warning(
            "读取转发 %s 失败，尝试记录：%s", forward_id, " | ".join(reasons[:6])
        )
        detail = "；".join(reasons[:2])
        raise ForwardMessageError(
            f"读取转发 {forward_id} 失败" + (f"（{detail}）" if detail else "")
        )

    async def _expand_forwards(
        self,
        event: AstrMessageEvent,
        nodes: Sequence[ForwardNode],
        history_index: dict[str, list[Any]] | None = None,
    ) -> list[ForwardNode]:
        """把树里所有惰性转发 ID 递归拉平，带环检测与拉取次数上限。"""

        limits = self._limits()
        pending = sorted(forward_tree_ids(nodes))
        seen: set[str] = set()
        resolved: dict[str, tuple[ForwardNode, ...]] = {}
        fetches = 0
        index = history_index if history_index is not None else {}
        used_index = history_index is not None
        while pending:
            forward_id = pending.pop(0)
            if forward_id in seen:
                continue
            seen.add(forward_id)
            if fetches >= limits.max_forward_fetch:
                raise ForwardMessageError(
                    f"嵌套转发拉取次数超过上限 {limits.max_forward_fetch}"
                )
            fetches += 1
            payload = None
            if forward_id in index:
                payload = {"messages": index[forward_id]}
            else:
                if not used_index:
                    # 第一次真正需要外部内容时再扫历史，避免每次读取都多打几页。
                    index = await self._build_history_forward_index(event)
                    used_index = True
                    if forward_id in index:
                        payload = {"messages": index[forward_id]}
            try:
                if payload is None:
                    payload = await self._fetch_forward_payload(event, forward_id)
                tree = parse_forward_payload(payload, limits)
            except ForwardMessageError as exc:
                logger.info("嵌套转发 %s 展开失败：%s", forward_id, str(exc)[:120])
                continue
            if forward_id in forward_tree_ids(tree):
                raise ForwardMessageError(f"检测到循环转发：{forward_id}")
            resolved[forward_id] = tree
            for nested_id in sorted(forward_tree_ids(tree)):
                if nested_id not in seen and nested_id not in resolved:
                    pending.append(nested_id)
        expanded = substitute_forward_trees(tuple(nodes), resolved)
        remaining = forward_tree_ids(expanded)
        if remaining:
            logger.warning(
                "以下转发保持未展开（协议端拿不到可信内容）：%s",
                ", ".join(sorted(remaining))[:160],
            )
        return list(expanded)

    def _segment_component(self, segment) -> Any:
        if segment.type == "text":
            return Plain(segment.value)
        if segment.type == "at":
            return At(qq=segment.value)
        if segment.type == "image":
            parsed = urlsplit(segment.value)
            if parsed.scheme.lower() in {"http", "https"}:
                return Image.fromURL(segment.value)
            try:
                image_path = Path(segment.value).resolve(strict=True)
                temp_root = Path(get_astrbot_temp_path()).resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise ForwardMessageError("本地图片不存在或路径无效") from exc
            if not image_path.is_file() or not image_path.is_relative_to(temp_root):
                raise ForwardMessageError(
                    "本地图片仅允许使用 AstrBot 临时目录中的文件"
                )
            return Image.fromFileSystem(image_path)
        if segment.type == "face":
            return Face(id=int(segment.value))
        if segment.type == "reply":
            return Reply(id=segment.value)
        if segment.type == "json":
            return Json(data=segment.value)
        if segment.type == "xml":
            return Plain(f"[XML卡片]")
        raise ForwardMessageError(f"不支持的消息段类型：{segment.type!r}")

    def _node_component(self, node: ForwardNode) -> Node:
        pure_forward = len(node.content) == 1 and node.content[0].type == "forward"
        content: list[Any] = []
        for segment in node.content:
            if segment.type == "forward":
                tree = segment.nested
                if tree is None or not tree.nodes:
                    raise ForwardMessageError("嵌套转发尚未展开")
                if pure_forward:
                    content.extend(self._node_component(item) for item in tree.nodes)
                    continue
                if not tree.forward_id:
                    raise ForwardMessageError(
                        "与普通消息混排的转发缺少 ID，QQ 无法表达该结构"
                    )
                content.append(Forward(id=tree.forward_id))
                continue
            content.append(self._segment_component(segment))
        return Node(uin=node.sender_id, name=node.sender_name, content=content)

    async def _send_forward(
        self,
        event: AstrMessageEvent,
        nodes: list[ForwardNode],
        presentation: Presentation | None = None,
        *,
        resolve_members: bool = True,
    ) -> int:
        peer_kind, peer_id = self._peer(event)
        sender_info = await self._check_access(event, peer_kind, peer_id)
        resolved_nodes = (
            await self._resolve_names(event, peer_kind, peer_id, nodes, sender_info)
            if resolve_members
            else list(nodes)
        )
        resolved_nodes = await self._expand_forwards(event, resolved_nodes)
        payload = build_forward_payload(
            peer_id,
            resolved_nodes,
            presentation or self._presentation(),
            self._limits(),
        )
        component_nodes = [self._node_component(node) for node in resolved_nodes]
        try:
            payload["messages"] = (await Nodes(nodes=component_nodes).to_dict())["messages"]
        except ForwardMessageError:
            raise
        except Exception as exc:
            raise ForwardMessageError("图片下载或转换失败") from exc
        if peer_kind == "group":
            action = "send_group_forward_msg"
        else:
            action = "send_private_forward_msg"
            payload.pop("group_id", None)
            payload["user_id"] = int(peer_id)
        response = await event.bot.call_action(
            action,
            **payload,
            **self._routing_params(event),
        )
        sent_id = ""
        if isinstance(response, dict):
            sent_id = str(response.get("message_id") or "").strip()
        if sent_id:
            # 自己发的转发先落本地缓存：NapCat 后续可能取不回 res_id 内容。
            self._cache_put(sent_id, payload["messages"])
        event._has_send_oper = True
        return len(resolved_nodes)

    @staticmethod
    def _command_qq_id(value: Any) -> str:
        qq = str(value or "").strip()
        if not qq.isdigit() or int(qq) <= 0 or len(qq) > 20:
            raise ForwardMessageError("@ 用户必须是有效的数字 QQ 号")
        return qq

    @classmethod
    def _normalize_command_text(cls, text: str) -> str:
        return _COMMAND_AT_PLACEHOLDER.sub(
            lambda match: cls._command_qq_id(match.group(1)),
            text,
        )

    @classmethod
    def _command_body(cls, event: AstrMessageEvent) -> str:
        components = event.get_messages()
        if components:
            chunks: list[str] = []
            prefix_pending = True
            self_id = str(event.get_self_id() or "").strip()
            for component in components:
                if isinstance(component, Plain):
                    text = cls._normalize_command_text(component.text)
                    if prefix_pending:
                        without_prefix = _COMMAND_PREFIX.sub("", text, count=1)
                        if without_prefix != text:
                            prefix_pending = False
                        text = without_prefix
                    chunks.append(text)
                elif isinstance(component, At):
                    qq = cls._command_qq_id(component.qq)
                    if prefix_pending and qq == self_id:
                        continue
                    chunks.append(qq)

            body = "".join(chunks).strip()
            if body or not prefix_pending:
                return body

        message_text = cls._normalize_command_text(event.get_message_str())
        return _COMMAND_PREFIX.sub("", message_text, count=1).strip()

    @classmethod
    def _read_argument(cls, event: AstrMessageEvent) -> str:
        text = cls._normalize_command_text(event.get_message_str() or "")
        return _READ_ARGUMENT_PREFIX.sub("", text, count=1).strip()

    @staticmethod
    def _raw_segments(event: AstrMessageEvent) -> list[Any]:
        """取 OneBot 原始消息段：里面才有引用回复的短 message_id 和合并转发卡片的 resid。"""

        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        segments = getattr(raw, "message", None) if raw is not None else None
        if segments is None:
            try:
                segments = raw.get("message") if isinstance(raw, dict) else None
            except Exception:  # noqa: BLE001
                segments = None
        return segments if isinstance(segments, list) else []

    @classmethod
    def _json_resids_in_chain(cls, chain: Any) -> list[str]:
        found: list[str] = []
        if not isinstance(chain, list):
            return found
        for segment in chain:
            if not isinstance(segment, dict) or segment.get("type") != "json":
                continue
            data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
            resid = cls._multimsg_resid(data.get("data"))
            if resid and resid not in found:
                found.append(resid)
        return found

    @staticmethod
    def _multimsg_resid(raw: Any) -> str:
        """从合并转发 json 卡片里取 resid，作为最后一种可直拉的 ID。"""

        if not isinstance(raw, str) or not raw.strip():
            return ""
        try:
            parsed = json.loads(raw.replace("&#44;", ","))
        except Exception:
            return ""
        if not isinstance(parsed, dict):
            return ""
        detail = parsed.get("meta", {}).get("detail", {})
        if not isinstance(detail, dict):
            return ""
        resid = detail.get("resid") or detail.get("resId") or ""
        return str(resid).strip()

    @classmethod
    def _forward_content_in_chain(cls, chain: Any) -> dict[str, Any] | None:
        if not isinstance(chain, list):
            return None
        for segment in chain:
            if not isinstance(segment, dict):
                continue
            data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
            if segment.get("type") not in {
                "forward",
                "forward_msg",
                "node",
                "nodes",
                "multimsg",
            }:
                continue
            content = data.get("content") or data.get("message")
            if isinstance(content, list) and content:
                return {"messages": content}
        return None

    async def _forward_payload_via_history(
        self,
        event: AstrMessageEvent,
        forward_id: str,
        reasons: list[str],
    ) -> Any | None:
        """扫最近群历史，找 id 命中且带内联内容的转发段。"""

        routing = self._routing_params(event)
        history_action: dict[str, Any] | None = None
        group_id = str(event.get_group_id() or "").strip()
        if group_id.isdigit():
            history_action = {"group_id": int(group_id)}
        else:
            peer_id = str(getattr(event, "get_sender_id", lambda: "")() or "").strip()
            if peer_id.isdigit():
                history_action = {"user_id": int(peer_id)}
        if history_action is None:
            return None
        action_name = (
            "get_group_msg_history" if "group_id" in history_action else "get_friend_msg_history"
        )
        page_size = 100
        cursor = ""
        for page in range(10):
            params: dict[str, Any] = {"count": page_size, "parse_mult_msg": True, **history_action}
            if cursor:
                params["message_seq"] = cursor
            try:
                history = await event.bot.call_action(action_name, **params, **routing)
            except Exception as exc:  # noqa: BLE001
                reasons.append(f"history(page{page})={type(exc).__name__}:{exc}")
                break
            messages = history.get("messages") if isinstance(history, dict) else None
            if not isinstance(messages, list) or not messages:
                break
            for message in messages:
                if not isinstance(message, dict):
                    continue
                chain = self._message_chain(message)
                for segment in chain:
                    if not isinstance(segment, dict):
                        continue
                    data = (
                        segment.get("data")
                        if isinstance(segment.get("data"), dict)
                        else {}
                    )
                    content = data.get("content") or data.get("message")
                    if not isinstance(content, list) or not content:
                        continue
                    segment_ids = [
                        str(data.get("id") or ""),
                        str(data.get("res_id") or ""),
                        str(data.get("message_id") or ""),
                    ]
                    message_id = str(message.get("message_id") or "")
                    if forward_id in segment_ids or forward_id == message_id:
                        return {"messages": content}
                    if forward_id in self._forward_ids_in_chain(content):
                        return {"messages": content}
            oldest = messages[-1]
            next_cursor = str(oldest.get("message_seq") or oldest.get("real_seq") or "")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return None

    async def _build_history_forward_index(
        self,
        event: AstrMessageEvent,
        pages: int = 3,
    ) -> dict[str, list[Any]]:
        """扫最近历史，把每条转发的各种 ID 映射到它的内联内容，供读取和嵌套展开复用。"""

        routing = self._routing_params(event)
        group_id = str(event.get_group_id() or "").strip()
        if group_id.isdigit():
            action_name = "get_group_msg_history"
            base: dict[str, Any] = {"group_id": int(group_id)}
        else:
            peer_id = str(getattr(event, "get_sender_id", lambda: "")() or "").strip()
            if not peer_id.isdigit():
                return {}
            action_name = "get_friend_msg_history"
            base = {"user_id": int(peer_id)}
        index: dict[str, list[Any]] = {}
        cursor = ""
        for _page in range(max(1, pages)):
            params: dict[str, Any] = {"count": 100, "parse_mult_msg": True, **base}
            if cursor:
                params["message_seq"] = cursor
            try:
                history = await event.bot.call_action(action_name, **params, **routing)
            except Exception:  # noqa: BLE001
                break
            messages = history.get("messages") if isinstance(history, dict) else None
            if not isinstance(messages, list) or not messages:
                break
            for message in messages:
                if not isinstance(message, dict):
                    continue
                message_id = str(message.get("message_id") or "")
                for segment in self._message_chain(message):
                    if not isinstance(segment, dict):
                        continue
                    if segment.get("type") not in {"forward", "forward_msg"}:
                        continue
                    data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
                    content = data.get("content") or data.get("message")
                    if not isinstance(content, list) or not content:
                        continue
                    keys = {
                        str(data.get("id") or ""),
                        str(data.get("res_id") or ""),
                        str(data.get("message_id") or ""),
                        message_id,
                    }
                    for key in keys:
                        if key:
                            index.setdefault(key, content)
            oldest = messages[-1]
            next_cursor = str(oldest.get("message_seq") or oldest.get("real_seq") or "")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return index

    async def _latest_forward_message_id(self, event: AstrMessageEvent) -> str:
        """在最近历史里找最新一条合并转发消息，返回它的 message_id。"""

        routing = self._routing_params(event)
        group_id = str(event.get_group_id() or "").strip()
        if group_id.isdigit():
            action_name = "get_group_msg_history"
            base = {"group_id": int(group_id)}
        else:
            peer_id = str(getattr(event, "get_sender_id", lambda: "")() or "").strip()
            if not peer_id.isdigit():
                return ""
            action_name = "get_friend_msg_history"
            base = {"user_id": int(peer_id)}
        try:
            history = await event.bot.call_action(
                action_name,
                count=100,
                parse_mult_msg=True,
                **base,
                **routing,
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("扫描最近转发失败：%s", str(exc)[:120])
            return ""
        messages = history.get("messages") if isinstance(history, dict) else None
        if not isinstance(messages, list):
            return ""
        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            for segment in self._message_chain(message):
                if not isinstance(segment, dict):
                    continue
                if segment.get("type") in {"forward", "forward_msg"}:
                    message_id = str(message.get("message_id") or "").strip()
                    if message_id.isdigit():
                        return message_id
        return ""

    def _forward_candidates(
        self,
        event: AstrMessageEvent,
        argument: str = "",
    ) -> list[str]:
        """收集所有可能的转发 ID：显式参数 > 本条消息里的转发 > 引用回复及其链路。"""

        candidates: list[str] = []

        def push(value: Any) -> None:
            text = str(value or "").strip()
            if text and text not in candidates:
                candidates.append(text)

        for segment in self._raw_segments(event):
            if not isinstance(segment, dict):
                continue
            data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
            segment_type = segment.get("type")
            if segment_type == "reply":
                push(data.get("id"))
            elif segment_type in {"forward", "forward_msg", "node", "nodes"}:
                push(data.get("res_id") or data.get("id") or data.get("message_id"))
            elif segment_type == "json":
                push(self._multimsg_resid(data.get("data")))
        if argument.strip():
            push(argument)
        for component in event.get_messages() or []:
            if isinstance(component, Forward):
                push(getattr(component, "id", ""))
            elif isinstance(component, Reply):
                for inner in getattr(component, "chain", None) or []:
                    if isinstance(inner, (Forward, Node)):
                        push(getattr(inner, "id", ""))
                    elif isinstance(inner, Json):
                        push(self._multimsg_resid(getattr(inner, "data", "")))
                push(getattr(component, "id", ""))
        return candidates

    async def _read_forward(
        self,
        event: AstrMessageEvent,
        argument: str = "",
    ) -> list[ForwardNode]:
        self._require_supported_platform(event)
        candidates = self._forward_candidates(event, argument)
        if not candidates:
            # 不引用也能读：扫最近历史，取最新一条合并转发消息来读。
            latest = await self._latest_forward_message_id(event)
            if latest:
                candidates = [latest]
                logger.info("未提供转发 ID，改用最近一条转发消息 %s", latest)
        if not candidates:
            raise ForwardMessageError(
                "没找到可读的合并转发：请引用一条转发、直接给出转发 ID，或先发一条转发消息"
            )
        failures: list[str] = []
        own_id = str(
            getattr(getattr(event, "message_obj", None), "message_id", "") or ""
        ).strip()
        for cached_id in [*candidates, own_id]:
            cached = self._cache_get(cached_id)
            if cached is None:
                continue
            try:
                nodes = parse_forward_payload(cached, self._limits())
                cached_nodes = await self._expand_forwards(event, nodes)
                logger.info("转发 %s 命中本地缓存", cached_id)
                return cached_nodes
            except ForwardMessageError as exc:
                failures.append(f"cache({cached_id}):{exc}")
        history_index = await self._build_history_forward_index(event)
        for forward_id in candidates:
            if not re.fullmatch(r"[0-9A-Za-z_\-+/=]{1,80}", forward_id):
                failures.append(f"{forward_id}:格式无效")
                continue
            # 历史里的内联内容优先（协议端对 res_id 的解析不稳），命中就直接用。
            if forward_id in history_index:
                try:
                    nodes = parse_forward_payload(
                        {"messages": history_index[forward_id]}, self._limits()
                    )
                    return await self._expand_forwards(event, nodes, history_index)
                except ForwardMessageError as exc:
                    failures.append(str(exc))
            history_payload = await self._forward_payload_via_history(
                event, forward_id, failures
            )
            if history_payload is not None:
                try:
                    nodes = parse_forward_payload(history_payload, self._limits())
                    return await self._expand_forwards(event, nodes, history_index)
                except ForwardMessageError as exc:
                    failures.append(str(exc))
            try:
                payload = await self._fetch_forward_payload(event, forward_id)
                nodes = parse_forward_payload(payload, self._limits())
                return await self._expand_forwards(event, nodes, history_index)
            except ForwardMessageError as exc:
                failures.append(str(exc))
                continue
        logger.warning(
            "转发读取失败，组件=%s，候选=%s",
            [
                (
                    type(component).__name__,
                    str(getattr(component, "id", ""))[:20],
                    [
                        (type(inner).__name__, str(getattr(inner, "id", ""))[:20])
                        for inner in (getattr(component, "chain", None) or [])
                    ],
                )
                for component in (event.get_messages() or [])
            ],
            candidates,
        )
        detail = "；".join(failures[:3]) or "无法解析这条转发消息"
        if any("空壳" in item or "跳过" in item for item in failures):
            detail += (
                "（协议端只返回了空节点：这条转发引用的内层消息很可能已被撤回或过期，"
                "NapCat 拿不到内容，不是插件解析问题）"
            )
        logger.info("转发读取失败明细：%s", " | ".join(failures[:6]))
        raise ForwardMessageError(detail)

    async def _debug_forward(
        self,
        event: AstrMessageEvent,
        argument: str = "",
    ) -> str:
        """把本次读取会用到的 ID 和每次尝试的结果都摊开，便于定位是哪个环节断的。"""

        lines: list[str] = []
        for component in event.get_messages() or []:
            lines.append(
                f"组件 {type(component).__name__} id={getattr(component, 'id', '')!r}"
            )
        candidates = self._forward_candidates(event, argument)
        lines.append("候选 ID: " + (", ".join(candidates) or "无"))
        for forward_id in candidates:
            if not re.fullmatch(r"[0-9A-Za-z_\-+/=]{1,80}", forward_id):
                lines.append(f"- {forward_id}: 格式无效")
                continue
            reasons: list[str] = []
            routing = self._routing_params(event)
            for key in ("message_id", "id"):
                try:
                    result = await event.bot.call_action(
                        "get_forward_msg", **{key: forward_id}, **routing
                    )
                    lines.append(
                        f"- get_forward_msg({key}) ok, keys={list(result)[:4] if isinstance(result, dict) else type(result).__name__}"
                    )
                    break
                except Exception as exc:  # noqa: BLE001
                    reasons.append(f"{key}={type(exc).__name__}:{exc}")
            else:
                lines.append(f"- get_forward_msg 全部失败: {' | '.join(reasons)}")
            try:
                message = await event.bot.call_action(
                    "get_msg", message_id=forward_id, **routing
                )
                chain = self._message_chain(message)
                lines.append(
                    f"- get_msg ok, segments={[segment.get('type') if isinstance(segment, dict) else type(segment).__name__ for segment in chain]}"
                )
            except Exception as exc:  # noqa: BLE001
                lines.append(f"- get_msg 失败: {type(exc).__name__}:{exc}")
        return "\n".join(lines)

    @classmethod
    def _legacy_body_and_images(
        cls,
        event: AstrMessageEvent,
    ) -> tuple[str, list[list[str]]]:
        parts: list[str] = []
        images: list[list[str]] = []
        current_text = ""
        current_images: list[str] = []
        prefix_pending = True
        self_id = str(event.get_self_id() or "").strip()

        for component in event.get_messages():
            if isinstance(component, Plain):
                text = cls._normalize_command_text(component.text)
                if prefix_pending:
                    without_prefix = _COMMAND_PREFIX.sub("", text, count=1)
                    if without_prefix != text:
                        prefix_pending = False
                    text = without_prefix
                chunks = text.split("|")
                current_text += chunks[0]
                for chunk in chunks[1:]:
                    parts.append(current_text.strip())
                    images.append(current_images)
                    current_text = chunk
                    current_images = []
            elif isinstance(component, At):
                qq = cls._command_qq_id(component.qq)
                if prefix_pending and qq == self_id:
                    continue
                current_text += f"{qq} "
            elif isinstance(component, Image):
                url = str(getattr(component, "url", "") or "").strip()
                if url:
                    current_images.append(url)

        if current_text.strip() or current_images:
            parts.append(current_text.strip())
            images.append(current_images)
        return " | ".join(part for part in parts if part), images

    async def _command_error(self, event: AstrMessageEvent, exc: Exception):
        if isinstance(exc, ForwardMessageError):
            message = str(exc)
        else:
            logger.exception("QQ Forward Message command failed")
            message = "QQ 平台调用异常"
        event.stop_event()
        return event.plain_result(f"QQ 合并转发助手：{message}")

    @filter.command("伪造转发")
    async def forward_command(self, event: AstrMessageEvent):
        """使用 JSON 或逐行 DSL 在当前会话（群聊或私聊）创建合并转发消息。"""
        try:
            nodes = parse_command_input(self._command_body(event), self._limits())
            await self._send_forward(event, nodes)
            event.stop_event()
        except Exception as exc:
            yield await self._command_error(event, exc)

    @filter.command("伪造消息")
    async def legacy_command(self, event: AstrMessageEvent):
        """兼容旧版"QQ号 内容 | QQ号 内容"命令。"""
        try:
            if not bool(self.config.get("enable_legacy_command", True)):
                raise ForwardMessageError("旧版命令已禁用，请使用 /伪造转发")
            body, images = self._legacy_body_and_images(event)
            if not body:
                body = self._command_body(event)
            nodes = parse_legacy(body, self._limits(), images)
            await self._send_forward(event, nodes)
            event.stop_event()
        except Exception as exc:
            yield await self._command_error(event, exc)

    @filter.command("查看转发")
    async def read_forward_command(self, event: AstrMessageEvent):
        """递归读取一条合并转发消息（含嵌套转发）并输出缩进文本。"""
        try:
            if not bool(self.config.get("enable_read_command", True)):
                raise ForwardMessageError("查看转发命令已禁用")
            nodes = await self._read_forward(event, self._read_argument(event))
            event.stop_event()
            yield event.plain_result(
                "合并转发内容：\n" + forward_nodes_to_text(nodes)
            )
        except Exception as exc:
            yield await self._command_error(event, exc)

    @filter.command("转发复制")
    async def copy_forward_command(self, event: AstrMessageEvent):
        """把引用到的合并转发（含嵌套层）在当前会话原样重发。"""
        try:
            if not bool(self.config.get("enable_copy_command", True)):
                raise ForwardMessageError("转发复制命令已禁用")
            nodes = await self._read_forward(event, self._read_argument(event))
            await self._send_forward(event, nodes, resolve_members=False)
            event.stop_event()
        except Exception as exc:
            yield await self._command_error(event, exc)

    @filter.command("转发调试")
    async def debug_forward_command(self, event: AstrMessageEvent):
        """输出读取转发时的候选 ID，以及每种调用方式的实际返回。"""
        try:
            report = await self._debug_forward(event, self._read_argument(event))
            event.stop_event()
            yield event.plain_result("转发调试：\n" + report)
        except Exception as exc:
            yield await self._command_error(event, exc)

    @filter.command("伪造帮助")
    async def help_command(self, event: AstrMessageEvent):
        """显示 QQ 合并转发助手命令格式。"""
        event.stop_event()
        yield event.plain_result(
            "QQ 合并转发助手用法：\n"
            "/伪造转发 后接 JSON 节点数组，或每行一条：QQ号/[At:QQ号][显示名]: 内容\n"
            "行内支持 [at:QQ号]、[image:https://...]、[face:ID]、[forward:转发ID]\n"
            "JSON 里可用 {\"type\":\"node\",...} 构造嵌套转发，或用 {\"type\":\"forward\",\"id\":\"...\"} 引用已有转发\n"
            "旧格式：/伪造消息 QQ号 内容 | QQ号 内容\n"
            "查看转发：/查看转发（引用一条合并转发消息）\n"
            "复制转发：/转发复制（读取引用转发后原样重发，保留嵌套层）\n"
            "群聊与私聊均可使用，发送目标就是当前会话"
        )

    @filter.llm_tool(name="send_qq_forward_message")
    async def send_qq_forward_message(
        self,
        event: AstrMessageEvent,
        nodes_json: str,
        prompt: str = "",
        summary: str = "",
        source: str = "",
    ) -> str:
        """在当前会话（群聊或私聊）发送受控的合并转发消息，不可指定其他目标。

        Args:
            nodes_json(string): JSON 节点数组；每项含 sender_id、可选 sender_name 和 content。content 字符串支持 [CQ:image,file=...] 和 [CQ:face,id=...]；content 数组里可以再放 {"type":"node","sender_id":...,"content":[...]} 形成嵌套转发，或用 {"type":"forward","id":"..."} 引用已存在的转发；本地图片必须位于 AstrBot data/temp 目录。
            prompt(string): 可选的转发卡片提示文字，留空使用插件配置。
            summary(string): 可选的转发卡片摘要，可使用 {count}，留空使用插件配置。
            source(string): 可选的转发卡片来源文字，留空使用插件配置。
        """
        try:
            if not bool(self.config.get("enable_llm_tool", True)):
                raise ForwardMessageError("LLM Tool 已禁用")
            nodes = parse_json_nodes(nodes_json, self._limits())
            count = await self._send_forward(
                event,
                nodes,
                self._presentation(prompt, summary, source),
            )
            return f"已向当前会话发送 {count} 个合并转发节点"
        except ForwardMessageError as exc:
            return f"发送失败：{exc}"
        except Exception:
            logger.exception("QQ Forward Message LLM tool failed")
            return "发送失败：QQ 平台调用异常"

    @filter.llm_tool(name="read_qq_forward_message")
    async def read_qq_forward_message(
        self,
        event: AstrMessageEvent,
        forward_id: str = "",
    ) -> str:
        """读取一条合并转发消息，返回 JSON 结构化结果（保留嵌套层级，并标明未展开的层）。

        Args:
            forward_id(string): 可选的转发 ID；留空则读取当前消息里携带的转发、引用回复的目标消息，或最近一条转发消息。返回的 JSON 含 forward_message/message_id/res_id/node_count/max_depth/fully_expanded/unresolved_ids/nodes；每个节点有 sender_id/sender_name/depth/segments，segments 里 type=forward 的项带 expanded 与嵌套的 nodes。
        """
        try:
            nodes = await self._read_forward(event, forward_id)
            payload = forward_nodes_to_struct(nodes)
            candidates = self._forward_candidates(event, forward_id)
            payload["requested_ids"] = candidates[:5]
            payload["text_preview"] = forward_nodes_to_text(nodes, max_lines=40)
            return json.dumps(payload, ensure_ascii=False)
        except ForwardMessageError as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
        except Exception:
            logger.exception("QQ Forward Message read tool failed")
            return "读取失败：QQ 平台调用异常"

    async def terminate(self) -> None:
        return None