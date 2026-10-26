import json

import pytest
from astrbot.api.message_components import At, Forward, Plain, Reply

from astrbot_plugin_forward_message.forward_message import (
    ForwardMessageError,
    ForwardNode,
    ForwardTree,
    Limits,
    Segment,
    build_forward_payload,
    forward_nodes_to_text,
    forward_tree_ids,
    parse_forward_payload,
    parse_json_nodes,
    substitute_forward_trees,
)
from astrbot_plugin_forward_message.main import SessionFakerPlugin


OUTER_PAYLOAD = {
    "status": "ok",
    "retcode": 0,
    "data": {
        "messages": [
            {
                "type": "node",
                "data": {
                    "user_id": 10001,
                    "nickname": "Alice",
                    "message": [
                        {"type": "text", "data": {"text": "outer "}},
                        {"type": "forward", "data": {"id": "777"}},
                    ],
                    "content": [],
                },
            }
        ]
    },
}

INNER_PAYLOAD = {
    "data": {
        "messages": [
            {
                "type": "node",
                "data": {
                    "user_id": 20002,
                    "nickname": "Bob",
                    "message": [
                        {"type": "text", "data": {"text": "inner"}},
                    ],
                },
            }
        ]
    },
}


class FakeBot:
    def __init__(self, forwards=None, members=None):
        self.forwards = forwards or {}
        self.members = members or {"999": {"nickname": "Caller", "role": "member"}}
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if action == "get_group_member_info":
            user_id = str(kwargs["user_id"])
            if user_id not in self.members:
                raise RuntimeError("not a member")
            return {"user_id": int(user_id), **self.members[user_id]}
        if action == "get_forward_msg":
            forward_id = str(kwargs.get("message_id") or kwargs.get("id") or "")
            if forward_id in self.forwards:
                return self.forwards[forward_id]
            raise RuntimeError("forward not found")
        if action == "send_group_forward_msg":
            return {"message_id": 1}
        raise AssertionError(f"unexpected action: {action}")


class FakeEvent:
    def __init__(self, bot, message="", messages=None, group_id="90001"):
        self.bot = bot
        self.message = message
        self.messages = messages or []
        self.group_id = group_id
        self._has_send_oper = False
        self.stopped = False

    def get_platform_name(self):
        return "aiocqhttp"

    def get_group_id(self):
        return self.group_id

    def get_sender_id(self):
        return "999"

    def get_self_id(self):
        return "888"

    def get_message_str(self):
        return self.message

    def get_messages(self):
        return self.messages

    def is_admin(self):
        return False

    def stop_event(self):
        self.stopped = True

    def plain_result(self, value):
        return value


def make_plugin(**config):
    plugin = object.__new__(SessionFakerPlugin)
    plugin.config = config
    return plugin


def test_json_node_segment_becomes_nested_forward():
    nodes = parse_json_nodes(
        json.dumps(
            [
                {
                    "sender_id": "10001",
                    "sender_name": "A",
                    "content": [
                        {
                            "type": "node",
                            "sender_id": "20002",
                            "sender_name": "B",
                            "content": "inner",
                        },
                    ],
                }
            ]
        )
    )

    assert [segment.type for segment in nodes[0].content] == ["forward"]
    nested = nodes[0].content[0].nested
    assert nested is not None
    assert nested.nodes[0].sender_id == "20002"
    assert nested.nodes[0].content[0].value == "inner"


def test_mixed_nested_node_without_id_is_rejected():
    with pytest.raises(ForwardMessageError, match="独占"):
        parse_json_nodes(
            json.dumps(
                [
                    {
                        "sender_id": "10001",
                        "content": [
                            {"type": "text", "text": "see "},
                            {
                                "type": "node",
                                "sender_id": "20002",
                                "content": "inner",
                            },
                        ],
                    }
                ]
            )
        )


def test_forward_id_token_is_lazy_reference():
    nodes = parse_json_nodes(
        '[{"sender_id":"10001","content":"hello[forward:777]"}]'
    )

    assert nodes[0].content[1].type == "forward"
    assert nodes[0].content[1].nested.forward_id == "777"
    assert forward_tree_ids(nodes) == {"777"}


def test_nested_forward_must_own_node_content():
    with pytest.raises(ForwardMessageError, match="独占"):
        parse_json_nodes(
            json.dumps(
                [
                    {
                        "sender_id": "10001",
                        "content": [
                            {"type": "text", "text": "x"},
                            {"type": "node", "sender_id": "20002", "content": "y"},
                        ],
                    }
                ]
            )
        )


def test_max_forward_depth_is_enforced():
    value = json.dumps(
        [
            {
                "sender_id": "10001",
                "content": [
                    {
                        "type": "node",
                        "sender_id": "20002",
                        "content": [
                            {
                                "type": "node",
                                "sender_id": "30003",
                                "content": "deep",
                            }
                        ],
                    }
                ],
            }
        ]
    )

    with pytest.raises(ForwardMessageError, match="嵌套深度"):
        parse_json_nodes(value, Limits(max_forward_depth=1))


def test_build_payload_emits_nested_node_segments():
    nodes = parse_json_nodes(
        json.dumps(
            [
                {
                    "sender_id": "10001",
                    "sender_name": "A",
                    "content": [
                        {
                            "type": "node",
                            "sender_id": "20002",
                            "sender_name": "B",
                            "content": "inner",
                        },
                    ],
                }
            ]
        )
    )

    payload = build_forward_payload("90001", nodes)

    assert payload["messages"][0]["data"]["content"] == [
        {
            "type": "node",
            "data": {
                "user_id": "20002",
                "nickname": "B",
                "content": [{"type": "text", "data": {"text": "inner"}}],
            },
        }
    ]


def test_build_payload_rejects_unexpanded_reference():
    nodes = parse_json_nodes(
        '[{"sender_id":"10001","sender_name":"A","content":[{"type":"forward","id":"777"}]}]'
    )

    with pytest.raises(ForwardMessageError, match="尚未展开"):
        build_forward_payload("90001", nodes)


def test_parse_forward_payload_keeps_nesting_and_fills_names():
    nodes = parse_forward_payload(OUTER_PAYLOAD)

    assert nodes[0].sender_name == "Alice"
    assert nodes[0].content[1].type == "forward"
    assert nodes[0].content[1].nested.forward_id == "777"


def test_parse_forward_payload_accepts_inline_nested_nodes():
    payload = {
        "data": {
            "messages": [
                {
                    "type": "node",
                    "data": {
                        "user_id": 10001,
                        "nickname": "Alice",
                        "message": [
                            {
                                "type": "node",
                                "data": {
                                    "user_id": 20002,
                                    "nickname": "Bob",
                                    "message": [{"type": "text", "data": {"text": "inner"}}],
                                },
                            }
                        ],
                    },
                }
            ]
        }
    }

    nodes = parse_forward_payload(payload)

    nested = nodes[0].content[0].nested
    assert nested is not None
    assert nested.nodes[0].sender_name == "Bob"


def test_substitute_forward_trees_replaces_lazy_ids():
    nodes = parse_json_nodes(
        '[{"sender_id":"10001","sender_name":"A","content":[{"type":"forward","id":"777"}]}]'
    )
    resolved = {
        "777": parse_forward_payload(INNER_PAYLOAD),
    }

    expanded = substitute_forward_trees(tuple(nodes), resolved)

    assert forward_tree_ids(expanded) == set()
    assert expanded[0].content[0].nested.nodes[0].sender_name == "Bob"


def test_forward_nodes_to_text_indents_nested_levels():
    nodes = substitute_forward_trees(
        tuple(parse_forward_payload(OUTER_PAYLOAD)),
        {"777": parse_forward_payload(INNER_PAYLOAD)},
    )

    text = forward_nodes_to_text(nodes)

    assert "Alice(10001): outer [转发消息]" in text
    assert "Bob(20002): inner" in text


@pytest.mark.asyncio
async def test_read_expands_nested_forward_recursively():
    bot = FakeBot(forwards={"100": OUTER_PAYLOAD, "777": INNER_PAYLOAD})
    event = FakeEvent(bot, messages=[Forward(id="100")])
    plugin = make_plugin()

    nodes = await plugin._read_forward(event)

    fetched = [str(call[1].get("message_id") or call[1].get("id")) for call in bot.calls if call[0] == "get_forward_msg"]
    assert fetched == ["100", "777"]
    text = forward_nodes_to_text(nodes)
    assert "Bob(20002): inner" in text


@pytest.mark.asyncio
async def test_read_detects_reference_cycles():
    looped = {
        "data": {
            "messages": [
                {
                    "type": "node",
                    "data": {
                        "user_id": 10001,
                        "nickname": "Alice",
                        "message": [{"type": "forward", "data": {"id": "777"}}],
                    },
                }
            ]
        }
    }
    bot = FakeBot(forwards={"100": looped, "777": looped})
    event = FakeEvent(bot, messages=[Forward(id="100")])
    plugin = make_plugin()

    with pytest.raises(ForwardMessageError, match="循环"):
        await plugin._read_forward(event)




class ChainBot(FakeBot):
    """get_forward_msg 直接失败，只能先 get_msg 再拿到真正的转发 ID。"""

    def __init__(self, forwards, message_segments):
        super().__init__(forwards)
        self.message_segments = message_segments

    async def call_action(self, action, **kwargs):
        if action == "get_forward_msg":
            self.calls.append((action, kwargs))
            forward_id = str(kwargs.get("message_id") or kwargs.get("id") or "")
            if forward_id in self.forwards:
                return self.forwards[forward_id]
            raise RuntimeError("message expired")
        if action == "get_msg":
            self.calls.append((action, kwargs))
            return {
                "message_id": 42,
                "message": self.message_segments,
            }
        return await super().call_action(action, **kwargs)


def test_candidates_put_reply_chain_forward_first():
    bot = FakeBot(forwards={})
    event = FakeEvent(bot, messages=[Reply(id="123", chain=[Forward(id="456")])])
    plugin = make_plugin()

    assert plugin._forward_candidates(event) == ["456", "123"]


@pytest.mark.asyncio
async def test_fetch_falls_back_to_get_msg_when_direct_lookup_fails():
    bot = ChainBot(
        forwards={"777": INNER_PAYLOAD},
        message_segments=[{"type": "forward", "data": {"id": "777"}}],
    )
    event = FakeEvent(bot, messages=[Reply(id="123")])
    plugin = make_plugin()

    nodes = await plugin._read_forward(event)

    actions = [call[0] for call in bot.calls]
    assert "get_msg" in actions
    assert forward_nodes_to_text(nodes).startswith("Bob(20002): inner")

class HistoryBot(FakeBot):
    """所有按 ID 的取法都失败，只能从最近群历史里扫到带内联内容的转发段。"""

    def __init__(self, history_messages):
        super().__init__({})
        self.history_messages = history_messages

    async def call_action(self, action, **kwargs):
        if action in {"get_forward_msg", "get_msg"}:
            self.calls.append((action, kwargs))
            raise RuntimeError("message expired")
        if action == "get_group_msg_history":
            self.calls.append((action, kwargs))
            return {"messages": self.history_messages}
        return await super().call_action(action, **kwargs)


@pytest.mark.asyncio
async def test_fetch_falls_back_to_group_history_scan():
    bot = HistoryBot(
        [
            {
                "message_id": 7,
                "message": [
                    {
                        "type": "forward",
                        "data": {
                            "id": "7699797351407036795",
                            "content": [
                                {
                                    "type": "node",
                                    "data": {
                                        "user_id": 20002,
                                        "nickname": "Bob",
                                        "message": [
                                            {"type": "text", "data": {"text": "inner"}}
                                        ],
                                    },
                                }
                            ],
                        },
                    }
                ],
            }
        ]
    )
    event = FakeEvent(bot, messages=[Reply(id="7699797351407036795")])
    plugin = make_plugin()

    nodes = await plugin._read_forward(event)

    assert "get_group_msg_history" in [call[0] for call in bot.calls]
    assert "Bob(20002): inner" in forward_nodes_to_text(nodes)




class CardBot(FakeBot):
    """合并转发在 parseMultMsg=false 下是一张 com.tencent.multimsg json 卡片。"""

    def __init__(self, forwards, cards):
        super().__init__(forwards)
        self.cards = cards

    async def call_action(self, action, **kwargs):
        if action == "get_forward_msg":
            self.calls.append((action, kwargs))
            forward_id = str(kwargs.get("message_id") or kwargs.get("id") or "")
            if forward_id in self.forwards:
                return self.forwards[forward_id]
            raise RuntimeError("message expired")
        if action == "get_msg":
            self.calls.append((action, kwargs))
            message_id = str(kwargs.get("message_id") or "")
            card = self.cards.get(message_id)
            if card is None:
                raise RuntimeError("message not found")
            return {"message_id": int(message_id), "message": [card]}
        return await super().call_action(action, **kwargs)


def _multimsg_card(resid):
    payload = {
        "app": "com.tencent.multimsg",
        "meta": {"detail": {"resid": resid, "news": []}},
    }
    return {"type": "json", "data": {"data": json.dumps(payload)}}


@pytest.mark.asyncio
async def test_resolves_forward_from_json_card_resid(tmp_path):
    bot = CardBot({"erdLPH": INNER_PAYLOAD}, {"555": _multimsg_card("erdLPH")})
    event = FakeEvent(bot, messages=[Reply(id="555")])
    plugin = make_plugin()
    plugin._cache_path = lambda: tmp_path / "forward_cache.json"

    nodes = await plugin._read_forward(event)

    actions = [call[0] for call in bot.calls]
    assert "get_msg" in actions
    assert any(
        call[0] == "get_forward_msg" and str(call[1].get("id")) == "erdLPH"
        for call in bot.calls
    )
    assert "Bob(20002): inner" in forward_nodes_to_text(nodes)


def test_raw_reply_segment_short_id_is_first_candidate():
    class Raw:
        message = [{"type": "reply", "data": {"id": "970792495"}}]

    class MessageObj:
        message_id = "1461362742"
        raw_message = Raw()

    event = FakeEvent(FakeBot(forwards={}), messages=[])
    event.message_obj = MessageObj()
    plugin = make_plugin()

    assert plugin._forward_candidates(event)[0] == "970792495"

@pytest.mark.asyncio
async def test_private_chat_can_read_forward(tmp_path):
    class PrivateBot(FakeBot):
        async def call_action(self, action, **kwargs):
            if action == "get_friend_msg_history":
                self.calls.append((action, kwargs))
                return {
                    "messages": [
                        {
                            "message_id": 7,
                            "message": [
                                {
                                    "type": "forward",
                                    "data": {
                                        "id": "7700898300578755988",
                                        "content": [
                                            {
                                                "type": "node",
                                                "data": {
                                                    "user_id": 20002,
                                                    "nickname": "Bob",
                                                    "message": [
                                                        {
                                                            "type": "text",
                                                            "data": {"text": "inner"},
                                                        }
                                                    ],
                                                },
                                            }
                                        ],
                                    },
                                }
                            ],
                        }
                    ]
                }
            return await super().call_action(action, **kwargs)

    bot = PrivateBot(forwards={})
    event = FakeEvent(bot, messages=[Reply(id="7")], group_id="")
    plugin = make_plugin()
    plugin._cache_path = lambda: tmp_path / "forward_cache.json"

    nodes = await plugin._read_forward(event)

    assert "get_friend_msg_history" in [call[0] for call in bot.calls]
    assert "Bob(20002): inner" in forward_nodes_to_text(nodes)


def test_res_id_is_preferred_over_numeric_msg_id():
    nodes = parse_forward_payload(
        {
            "messages": [
                {
                    "type": "node",
                    "data": {
                        "user_id": 10001,
                        "nickname": "Alice",
                        "content": [
                            {
                                "type": "forward",
                                "data": {
                                    "id": "7700902369030897447",
                                    "res_id": "RxtUpYf+nXhC7f+L57yrTT2w=",
                                },
                            }
                        ],
                    },
                }
            ]
        }
    )

    assert nodes[0].content[0].nested.forward_id == "RxtUpYf+nXhC7f+L57yrTT2w="


@pytest.mark.asyncio
async def test_fetch_budget_is_enforced():
    chain = {}
    for index in range(5):
        chain[str(index)] = {
            "data": {
                "messages": [
                    {
                        "type": "node",
                        "data": {
                            "user_id": 10001,
                            "nickname": "Alice",
                            "message": [
                                {"type": "forward", "data": {"id": str(index + 1)}}
                            ],
                        },
                    }
                ]
            }
        }
    bot = FakeBot(forwards=chain)
    event = FakeEvent(bot, messages=[Forward(id="0")])
    plugin = make_plugin(max_forward_fetch=2)

    with pytest.raises(ForwardMessageError, match="上限"):
        await plugin._read_forward(event)


@pytest.mark.asyncio
async def test_copy_command_resends_nested_tree():
    bot = FakeBot(
        forwards={"100": OUTER_PAYLOAD, "777": INNER_PAYLOAD},
        members={"999": {"nickname": "Caller", "role": "member"}},
    )
    event = FakeEvent(bot, message="/转发复制", messages=[Reply(id="100")])
    plugin = make_plugin()

    results = [item async for item in plugin.copy_forward_command(event)]

    assert results == []
    payload = next(kwargs for action, kwargs in bot.calls if action == "send_group_forward_msg")
    outer = payload["messages"][0]["data"]["content"]
    # 混排节点无法用嵌套 node 段表达，退回 forward 段携带 ID（NapCat 会转成 multimsg）
    assert outer[1] == {"type": "forward", "data": {"id": "777"}}


@pytest.mark.asyncio
async def test_read_command_renders_tree_without_sending():
    bot = FakeBot(forwards={"100": OUTER_PAYLOAD, "777": INNER_PAYLOAD})
    event = FakeEvent(bot, message="/查看转发", messages=[Forward(id="100")])
    plugin = make_plugin()

    results = [item async for item in plugin.read_forward_command(event)]

    assert len(results) == 1
    assert "合并转发内容" in results[0]
    assert "Bob(20002): inner" in results[0]
    assert not any(call[0] == "send_group_forward_msg" for call in bot.calls)


@pytest.mark.asyncio
async def test_lazy_reference_is_expanded_before_sending():
    bot = FakeBot(
        forwards={"777": INNER_PAYLOAD},
        members={"999": {"nickname": "Caller", "role": "member"}, "10001": {"card": "A", "role": "member"}},
    )
    event = FakeEvent(bot)
    plugin = make_plugin()
    nodes = parse_json_nodes(
        '[{"sender_id":"10001","content":[{"type":"forward","id":"777"}]}]'
    )

    await plugin._send_forward(event, nodes)

    payload = next(kwargs for action, kwargs in bot.calls if action == "send_group_forward_msg")
    assert payload["messages"][0]["data"]["content"][0]["type"] == "node"
    assert payload["messages"][0]["data"]["content"][0]["data"]["nickname"] == "Bob"
