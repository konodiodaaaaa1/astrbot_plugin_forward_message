from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Face, Image, Node, Nodes, Plain
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

from .forward_message import (
    ForwardMessageError,
    ForwardNode,
    Limits,
    Presentation,
    build_forward_payload,
    parse_command_input,
    parse_json_nodes,
    parse_legacy,
    referenced_member_ids,
    resolve_member_names,
)


PLUGIN_NAME = "QQ Forward Message"
SUPPORTED_PLATFORM = "aiocqhttp"
_COMMAND_PREFIX = re.compile(r"^\s*/?(?:伪造转发|伪造消息)\s*", re.IGNORECASE)
_COMMAND_AT_PLACEHOLDER = re.compile(r"\[At:([^\]\r\n]+)\]")


class SessionFakerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config if config is not None else {}

    def _config_int(self, key: str, default: int) -> int:
        try:
            return max(1, int(self.config.get(key, default)))
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
    def _require_group_event(event: AstrMessageEvent) -> str:
        if str(event.get_platform_name()).lower() != SUPPORTED_PLATFORM:
            raise ForwardMessageError("仅支持 AIOCQHTTP/NapCat 平台")
        group_id = str(event.get_group_id() or "").strip()
        if not group_id.isdigit() or int(group_id) <= 0:
            raise ForwardMessageError("仅支持当前 QQ 群聊，私聊不可用")
        return group_id

    async def _member_info(
        self,
        event: AstrMessageEvent,
        group_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        try:
            result = await event.bot.call_action(
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(user_id),
                no_cache=False,
                **self._routing_params(event),
            )
        except Exception as exc:
            raise ForwardMessageError(f"QQ {user_id} 不是当前群成员或资料不可用") from exc
        if not isinstance(result, dict) or str(result.get("user_id", "")) != user_id:
            raise ForwardMessageError(f"QQ {user_id} 不是当前群成员或资料不可用")
        return result

    async def _check_access(
        self,
        event: AstrMessageEvent,
        group_id: str,
    ) -> dict[str, Any]:
        whitelist = self._group_whitelist()
        if whitelist and group_id not in whitelist:
            raise ForwardMessageError("当前群不在插件白名单中")

        sender_id = str(event.get_sender_id() or "").strip()
        if not sender_id.isdigit():
            raise ForwardMessageError("无法识别当前发送者")
        sender_info = await self._member_info(event, group_id, sender_id)
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
        group_id: str,
        nodes: list[ForwardNode],
        sender_info: dict[str, Any],
    ) -> list[ForwardNode]:
        names: dict[str, str] = {}
        event_sender = str(sender_info.get("user_id", ""))
        if event_sender:
            names[event_sender] = self._member_name(sender_info, event_sender)
        for user_id in sorted(referenced_member_ids(nodes) - names.keys(), key=int):
            info = await self._member_info(event, group_id, user_id)
            names[user_id] = self._member_name(info, user_id)
        return resolve_member_names(nodes, names)

    async def _send_forward(
        self,
        event: AstrMessageEvent,
        nodes: list[ForwardNode],
        presentation: Presentation | None = None,
    ) -> int:
        group_id = self._require_group_event(event)
        sender_info = await self._check_access(event, group_id)
        resolved_nodes = await self._resolve_names(event, group_id, nodes, sender_info)
        payload = build_forward_payload(
            group_id,
            resolved_nodes,
            presentation or self._presentation(),
            self._limits(),
        )
        component_nodes = []
        for node in resolved_nodes:
            content = []
            for segment in node.content:
                if segment.type == "text":
                    content.append(Plain(segment.value))
                elif segment.type == "at":
                    content.append(At(qq=segment.value))
                elif segment.type == "image":
                    parsed = urlsplit(segment.value)
                    if parsed.scheme.lower() in {"http", "https"}:
                        content.append(Image.fromURL(segment.value))
                    else:
                        try:
                            image_path = Path(segment.value).resolve(strict=True)
                            temp_root = Path(get_astrbot_temp_path()).resolve(strict=True)
                        except (OSError, RuntimeError) as exc:
                            raise ForwardMessageError("本地图片不存在或路径无效") from exc
                        if not image_path.is_file() or not image_path.is_relative_to(temp_root):
                            raise ForwardMessageError(
                                "本地图片仅允许使用 AstrBot 临时目录中的文件"
                            )
                        content.append(Image.fromFileSystem(image_path))
                elif segment.type == "face":
                    content.append(Face(id=int(segment.value)))
            component_nodes.append(
                Node(
                    uin=node.sender_id,
                    name=node.sender_name,
                    content=content,
                )
            )
        try:
            payload["messages"] = (await Nodes(nodes=component_nodes).to_dict())["messages"]
        except Exception as exc:
            raise ForwardMessageError("图片下载或转换失败") from exc
        await event.bot.call_action(
            "send_group_forward_msg",
            **payload,
            **self._routing_params(event),
        )
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
        """使用 JSON 或逐行 DSL 创建当前群的合并转发消息。"""
        try:
            nodes = parse_command_input(self._command_body(event), self._limits())
            await self._send_forward(event, nodes)
            event.stop_event()
        except Exception as exc:
            yield await self._command_error(event, exc)

    @filter.command("伪造消息")
    async def legacy_command(self, event: AstrMessageEvent):
        """兼容旧版“QQ号 内容 | QQ号 内容”命令。"""
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

    @filter.command("伪造帮助")
    async def help_command(self, event: AstrMessageEvent):
        """显示 QQ 合并转发助手命令格式。"""
        event.stop_event()
        yield event.plain_result(
            "QQ 合并转发助手用法：\n"
            "/伪造转发 后接 JSON 节点数组，或每行一条：QQ号/[At:QQ号][显示名]: 内容\n"
            "行内支持 [at:QQ号]、[image:https://...]、[face:ID]\n"
            "旧格式：/伪造消息 QQ号/[At:QQ号] 内容 | QQ号/[At:QQ号] 内容"
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
        """在当前 QQ 群发送受控的合并转发消息，不可指定目标群。

        Args:
            nodes_json(string): JSON 节点数组；每项含 sender_id、可选 sender_name 和 content。content 字符串支持 [CQ:image,file=...] 和 [CQ:face,id=...]；本地图片必须位于 AstrBot data/temp 目录。
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
            return f"已向当前群发送 {count} 个合并转发节点"
        except ForwardMessageError as exc:
            return f"发送失败：{exc}"
        except Exception:
            logger.exception("QQ Forward Message LLM tool failed")
            return "发送失败：QQ 平台调用异常"

    async def terminate(self) -> None:
        return None
