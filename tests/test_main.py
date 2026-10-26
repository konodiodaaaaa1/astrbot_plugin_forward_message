import json
from pathlib import Path

import pytest
from astrbot.api.message_components import At, AtAll, Image, Plain

import astrbot_plugin_forward_message.main as sessionfaker_main
from astrbot_plugin_forward_message.forward_message import (
    ForwardMessageError,
    parse_dsl,
    parse_json_nodes,
)
from astrbot_plugin_forward_message.main import SessionFakerPlugin


class FakeBot:
    def __init__(self, members):
        self.members = members
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if action == "get_group_member_info":
            user_id = str(kwargs["user_id"])
            if user_id not in self.members:
                raise RuntimeError("not a member")
            return {"user_id": int(user_id), **self.members[user_id]}
        if action == "send_group_forward_msg":
            return {"message_id": 1}
        if action == "send_private_forward_msg":
            return {"message_id": 1}
        if action == "get_stranger_info":
            user_id = str(kwargs["user_id"])
            return {"user_id": int(user_id), "nickname": f"Stranger {user_id}"}
        raise AssertionError(f"unexpected action: {action}")


class FakeEvent:
    def __init__(
        self,
        bot,
        message="",
        group_id="90001",
        sender_id="999",
        astrbot_admin=False,
        platform="aiocqhttp",
        messages=None,
    ):
        self.bot = bot
        self.message = message
        self.group_id = group_id
        self.sender_id = sender_id
        self.astrbot_admin = astrbot_admin
        self.platform = platform
        self.messages = messages or []
        self._has_send_oper = False
        self.stopped = False

    def get_platform_name(self):
        return self.platform

    def get_group_id(self):
        return self.group_id

    def get_sender_id(self):
        return self.sender_id

    def get_self_id(self):
        return "888"

    def get_message_str(self):
        return self.message

    def get_messages(self):
        return self.messages

    def is_admin(self):
        return self.astrbot_admin

    def stop_event(self):
        self.stopped = True

    def plain_result(self, value):
        return value


def make_plugin(**config):
    plugin = object.__new__(SessionFakerPlugin)
    plugin.config = config
    return plugin


def members(sender_role="member"):
    return {
        "999": {"nickname": "Caller", "role": sender_role},
        "10001": {"card": "Card A", "nickname": "A", "role": "member"},
        "10002": {"card": "", "nickname": "Nick B", "role": "member"},
    }


@pytest.mark.asyncio
async def test_send_path_uses_current_group_resolves_members_and_marks_event():
    bot = FakeBot(members())
    event = FakeEvent(bot)
    plugin = make_plugin()

    count = await plugin._send_forward(event, parse_dsl("10001: hi[at:10002]"))

    assert count == 1
    assert event._has_send_oper is True
    send_calls = [call for call in bot.calls if call[0] == "send_group_forward_msg"]
    assert len(send_calls) == 1
    payload = send_calls[0][1]
    assert payload["group_id"] == 90001
    assert payload["self_id"] == 888
    assert payload["messages"][0]["data"]["nickname"] == "Card A"
    member_ids = {
        str(call[1]["user_id"])
        for call in bot.calls
        if call[0] == "get_group_member_info"
    }
    assert member_ids == {"999", "10001", "10002"}


@pytest.mark.asyncio
async def test_send_path_serializes_image_and_face_for_forward_nodes(monkeypatch):
    async def fake_convert_to_base64(image):
        assert image.file == "https://example.com/a.png"
        return "encoded-image"

    monkeypatch.setattr(Image, "convert_to_base64", fake_convert_to_base64)
    bot = FakeBot(members())
    event = FakeEvent(bot)
    plugin = make_plugin()

    await plugin._send_forward(
        event,
        parse_dsl("10001: [image:https://example.com/a.png][face:14]"),
    )

    payload = next(
        kwargs for action, kwargs in bot.calls if action == "send_group_forward_msg"
    )
    assert payload["messages"][0]["data"]["content"] == [
        {"type": "image", "data": {"file": "base64://encoded-image"}},
        {"type": "face", "data": {"id": 14}},
    ]


@pytest.mark.asyncio
async def test_send_path_accepts_cq_image_only_from_astrbot_temp(
    monkeypatch,
    tmp_path,
):
    astrbot_temp = tmp_path / "data" / "temp"
    astrbot_temp.mkdir(parents=True)
    image_path = astrbot_temp / "generated.jpg"
    image_path.write_bytes(b"image")

    async def fake_convert_to_base64(image):
        assert Path(image.path) == image_path.resolve()
        return "local-image"

    monkeypatch.setattr(
        sessionfaker_main,
        "get_astrbot_temp_path",
        lambda: str(astrbot_temp),
    )
    monkeypatch.setattr(Image, "convert_to_base64", fake_convert_to_base64)
    bot = FakeBot(members())
    plugin = make_plugin()
    nodes = parse_json_nodes(
        json.dumps(
            [
                {
                    "sender_id": "10001",
                    "content": f"image\n[CQ:image,file={image_path}]",
                },
                {
                    "sender_id": "10002",
                    "content": "face\n[CQ:face,id=14]",
                },
            ]
        )
    )

    await plugin._send_forward(FakeEvent(bot), nodes)

    payload = next(
        kwargs for action, kwargs in bot.calls if action == "send_group_forward_msg"
    )
    assert payload["messages"][0]["data"]["content"][1] == {
        "type": "image",
        "data": {"file": "base64://local-image"},
    }
    assert payload["messages"][1]["data"]["content"][1] == {
        "type": "face",
        "data": {"id": 14},
    }


@pytest.mark.asyncio
async def test_send_path_rejects_cq_image_outside_astrbot_temp(monkeypatch, tmp_path):
    astrbot_temp = tmp_path / "data" / "temp"
    astrbot_temp.mkdir(parents=True)
    outside_image = tmp_path / "secret.jpg"
    outside_image.write_bytes(b"not allowed")
    monkeypatch.setattr(
        sessionfaker_main,
        "get_astrbot_temp_path",
        lambda: str(astrbot_temp),
    )
    plugin = make_plugin()
    nodes = parse_json_nodes(
        json.dumps(
            [
                {
                    "sender_id": "10001",
                    "content": f"[CQ:image,file={outside_image}]",
                }
            ]
        )
    )

    with pytest.raises(ForwardMessageError, match="临时目录"):
        await plugin._send_forward(FakeEvent(FakeBot(members())), nodes)


@pytest.mark.asyncio
async def test_unknown_sender_falls_back_to_placeholder_name():
    bot = FakeBot(members())
    event = FakeEvent(bot)
    plugin = make_plugin()

    await plugin._send_forward(event, parse_dsl("10003[Override]: hi"))

    payload = next(
        kwargs for action, kwargs in bot.calls if action == "send_group_forward_msg"
    )
    assert payload["messages"][0]["data"]["nickname"] == "Override"


@pytest.mark.asyncio
async def test_private_chat_sends_private_forward():
    plugin = make_plugin()
    private_bot = FakeBot(members())
    event = FakeEvent(private_bot, group_id="")
    await plugin._send_forward(event, parse_dsl("10001: hi"))
    assert event._has_send_oper is True
    actions = [call[0] for call in private_bot.calls]
    assert "send_private_forward_msg" in actions
    assert "send_group_forward_msg" not in actions
    payload = next(
        kwargs for action, kwargs in private_bot.calls if action == "send_private_forward_msg"
    )
    assert payload["user_id"] == 999
    assert "group_id" not in payload
    names = {call[1].get("user_id") for call in private_bot.calls if call[0] == "get_stranger_info"}
    assert 10001 in names


@pytest.mark.asyncio
async def test_other_platform_events_are_rejected_without_actions():
    plugin = make_plugin()
    other_bot = FakeBot(members())
    with pytest.raises(ForwardMessageError, match="AIOCQHTTP"):
        await plugin._send_forward(
            FakeEvent(other_bot, platform="telegram"),
            parse_dsl("10001: hi"),
        )
    assert other_bot.calls == []


@pytest.mark.asyncio
async def test_whitelist_is_checked_before_member_or_permission_queries():
    bot = FakeBot(members())
    event = FakeEvent(bot)
    plugin = make_plugin(group_whitelist="123,456")
    with pytest.raises(ForwardMessageError, match="白名单"):
        await plugin._send_forward(event, parse_dsl("10001: hi"))
    assert bot.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "role", "astrbot_admin", "allowed"),
    [
        ("everyone", "member", False, True),
        ("astrbot_admin", "member", True, True),
        ("astrbot_admin", "owner", False, False),
        ("qq_group_admin", "admin", False, True),
        ("qq_group_admin", "member", True, False),
        ("admin_only", "member", True, True),
        ("admin_only", "owner", False, True),
        ("admin_only", "member", False, False),
    ],
)
async def test_permission_modes(mode, role, astrbot_admin, allowed):
    bot = FakeBot(members(role))
    event = FakeEvent(bot, astrbot_admin=astrbot_admin)
    plugin = make_plugin(permission_mode=mode)
    if allowed:
        await plugin._send_forward(event, parse_dsl("10001: hi"))
        assert event._has_send_oper is True
    else:
        with pytest.raises(ForwardMessageError, match="权限"):
            await plugin._send_forward(event, parse_dsl("10001: hi"))
        assert not any(call[0] == "send_group_forward_msg" for call in bot.calls)


@pytest.mark.asyncio
async def test_command_and_tool_converge_on_send_service():
    command_bot = FakeBot(members())
    command_event = FakeEvent(
        command_bot,
        message='/伪造转发 [{"sender_id":"10001","content":"hello"}]',
    )
    plugin = make_plugin()
    command_results = [item async for item in plugin.forward_command(command_event)]
    assert command_results == []
    assert command_event.stopped is True
    assert command_event._has_send_oper is True

    tool_bot = FakeBot(members())
    tool_event = FakeEvent(tool_bot)
    result = await plugin.send_qq_forward_message(
        tool_event,
        json.dumps([{"sender_id": "10002", "content": "hello"}]),
    )
    assert result == "已向当前会话发送 1 个合并转发节点"
    assert tool_event._has_send_oper is True
    assert [call[0] for call in tool_bot.calls].count("send_group_forward_msg") == 1


@pytest.mark.asyncio
async def test_forward_command_treats_at_components_as_qq_ids():
    bot = FakeBot(members())
    event = FakeEvent(
        bot,
        message="/伪造转发 @Card A(10001): hello @Nick B(10002)",
        messages=[
            At(qq="888", name="Bot"),
            Plain("/伪造转发 "),
            At(qq="10001", name="Card A"),
            Plain(": hello[at:"),
            At(qq="10002", name="Nick B"),
            Plain("]"),
        ],
    )
    plugin = make_plugin()

    results = [item async for item in plugin.forward_command(event)]

    assert results == []
    payload = next(
        kwargs for action, kwargs in bot.calls if action == "send_group_forward_msg"
    )
    assert payload["messages"][0]["data"]["user_id"] == "10001"
    assert payload["messages"][0]["data"]["content"] == [
        {"type": "text", "data": {"text": "hello"}},
        {"type": "at", "data": {"qq": "10002"}},
    ]


@pytest.mark.asyncio
async def test_forward_json_command_accepts_at_sender_component():
    bot = FakeBot(members())
    event = FakeEvent(
        bot,
        messages=[
            Plain('/伪造转发 [{"sender_id":'),
            At(qq="10002", name="Nick B"),
            Plain(',"content":"hello"}]'),
        ],
    )
    plugin = make_plugin()

    results = [item async for item in plugin.forward_command(event)]

    assert results == []
    payload = next(
        kwargs for action, kwargs in bot.calls if action == "send_group_forward_msg"
    )
    assert payload["messages"][0]["data"]["user_id"] == "10002"


@pytest.mark.asyncio
async def test_legacy_command_accepts_at_sender_components():
    bot = FakeBot(members())
    event = FakeEvent(
        bot,
        messages=[
            Plain("/伪造消息 "),
            At(qq="10001", name="Card A"),
            Plain(" first | "),
            At(qq="10002", name="Nick B"),
            Plain(" second"),
        ],
    )
    plugin = make_plugin()

    results = [item async for item in plugin.legacy_command(event)]

    assert results == []
    payload = next(
        kwargs for action, kwargs in bot.calls if action == "send_group_forward_msg"
    )
    assert [node["data"]["user_id"] for node in payload["messages"]] == [
        "10001",
        "10002",
    ]


@pytest.mark.asyncio
async def test_legacy_at_sender_does_not_require_plain_text_leading_space():
    group_members = members()
    group_members["2116183730"] = {
        "card": "Mentioned User",
        "nickname": "Mentioned User",
        "role": "member",
    }
    bot = FakeBot(group_members)
    event = FakeEvent(
        bot,
        messages=[
            Plain("/伪造消息 "),
            At(qq="2116183730", name="Mentioned User"),
            Plain("1"),
        ],
    )
    plugin = make_plugin()

    results = [item async for item in plugin.legacy_command(event)]

    assert results == []
    payload = next(
        kwargs for action, kwargs in bot.calls if action == "send_group_forward_msg"
    )
    assert payload["messages"][0]["data"]["user_id"] == "2116183730"
    assert payload["messages"][0]["data"]["content"] == [
        {"type": "text", "data": {"text": "1"}},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message_components",
    [
        [Plain("/伪造消息 [At:2116183730] 1")],
        [],
    ],
)
async def test_legacy_command_accepts_textual_at_placeholder(message_components):
    group_members = members()
    group_members["2116183730"] = {
        "card": "Placeholder User",
        "nickname": "Placeholder User",
        "role": "member",
    }
    bot = FakeBot(group_members)
    event = FakeEvent(
        bot,
        message="/伪造消息 [At:2116183730] 1",
        messages=message_components,
    )
    plugin = make_plugin()

    results = [item async for item in plugin.legacy_command(event)]

    assert results == []
    payload = next(
        kwargs for action, kwargs in bot.calls if action == "send_group_forward_msg"
    )
    assert payload["messages"][0]["data"]["user_id"] == "2116183730"
    assert payload["messages"][0]["data"]["content"] == [
        {"type": "text", "data": {"text": "1"}},
    ]


@pytest.mark.asyncio
async def test_command_rejects_at_all_as_a_qq_id():
    bot = FakeBot(members())
    event = FakeEvent(
        bot,
        messages=[Plain("/伪造转发 "), AtAll(), Plain(": hello")],
    )
    plugin = make_plugin()

    results = [item async for item in plugin.forward_command(event)]

    assert results == ["QQ 合并转发助手：@ 用户必须是有效的数字 QQ 号"]
    assert not any(action == "send_group_forward_msg" for action, _ in bot.calls)


@pytest.mark.asyncio
async def test_tool_sends_even_when_sender_is_not_a_group_member():
    bot = FakeBot(members())
    event = FakeEvent(bot)
    plugin = make_plugin()
    result = await plugin.send_qq_forward_message(
        event,
        '[{"sender_id":"123456","content":"hello"}]',
    )
    assert result == "已向当前会话发送 1 个合并转发节点"
    assert event._has_send_oper is True
    payload = next(
        kwargs for action, kwargs in bot.calls if action == "send_group_forward_msg"
    )
    assert payload["messages"][0]["data"]["nickname"] == "Stranger 123456"


@pytest.mark.asyncio
async def test_command_masks_unexpected_platform_errors():
    class BrokenBot(FakeBot):
        async def call_action(self, action, **kwargs):
            if action == "send_group_forward_msg":
                raise RuntimeError("sensitive transport detail")
            return await super().call_action(action, **kwargs)

    event = FakeEvent(
        BrokenBot(members()),
        message='/伪造转发 [{"sender_id":"10001","content":"hello"}]',
    )
    plugin = make_plugin()
    results = [item async for item in plugin.forward_command(event)]
    assert results == ["QQ 合并转发助手：QQ 平台调用异常"]
    assert "sensitive" not in results[0]
