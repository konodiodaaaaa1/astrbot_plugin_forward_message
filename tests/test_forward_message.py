import json

import pytest

from astrbot_plugin_forward_message.forward_message import (
    ForwardMessageError,
    Limits,
    Presentation,
    build_forward_payload,
    parse_command_input,
    parse_dsl,
    parse_json_nodes,
    parse_legacy,
    referenced_member_ids,
    resolve_member_names,
)


def test_parse_json_all_segment_types():
    nodes = parse_json_nodes(
        json.dumps(
            [
                {
                    "sender_id": "10001",
                    "sender_name": "Alice",
                    "content": [
                        {"type": "text", "text": "hello"},
                        {"type": "at", "qq": "10002"},
                        {"type": "image", "url": "https://example.com/a.png"},
                        {"type": "face", "id": 14},
                    ],
                }
            ]
        )
    )

    assert nodes[0].sender_id == "10001"
    assert [segment.type for segment in nodes[0].content] == [
        "text",
        "at",
        "image",
        "face",
    ]


def test_face_zero_is_valid():
    nodes = parse_json_nodes(
        '[{"sender_id":"10001","content":[{"type":"face","id":0}]}]'
    )
    assert nodes[0].content[0].value == "0"


def test_parse_dsl_inline_tokens_and_escape():
    nodes = parse_dsl(
        "10001[Alice]: hello[at:10002]\\[face:14]\n"
        "10002: [image:https://example.com/a.png][face:14]"
    )

    assert nodes[0].sender_name == "Alice"
    assert [(item.type, item.value) for item in nodes[0].content] == [
        ("text", "hello"),
        ("at", "10002"),
        ("text", "[face:14]"),
    ]
    assert [item.type for item in nodes[1].content] == ["image", "face"]


def test_command_input_selects_json_or_dsl():
    assert parse_command_input('[{"sender_id":1,"content":"x"}]')[0].sender_id == "1"
    assert parse_command_input("2: y")[0].sender_id == "2"


def test_json_string_content_expands_inline_media_tokens():
    nodes = parse_json_nodes(
        '[{"sender_id":"10001","content":"[image:https://example.com/a.png][face:14]"}]'
    )

    assert [(item.type, item.value) for item in nodes[0].content] == [
        ("image", "https://example.com/a.png"),
        ("face", "14"),
    ]


def test_json_string_content_expands_onebot_cq_image_and_face():
    local_image = (
        r"D:\AI\AstrBotLauncher-0.1.5.4\AstrBot\data\temp"
        r"\media_image_2d5aebc6f22e4e7499bea55221616973.jpg"
    )
    nodes = parse_json_nodes(
        json.dumps(
            [
                {
                    "sender_id": "528217068",
                    "content": (
                        "[模拟消息] 表情包测试：\n"
                        f"[CQ:image,file={local_image}]"
                    ),
                },
                {
                    "sender_id": "528217068",
                    "content": (
                        "[模拟消息] QQ 自带 FaceID 表情测试：\n"
                        "[CQ:face,id=14]"
                    ),
                },
            ]
        )
    )

    assert [(item.type, item.value) for item in nodes[0].content] == [
        ("text", "[模拟消息] 表情包测试：\n"),
        ("image", local_image),
    ]
    assert [(item.type, item.value) for item in nodes[1].content] == [
        ("text", "[模拟消息] QQ 自带 FaceID 表情测试：\n"),
        ("face", "14"),
    ]


def test_legacy_images_stay_with_corresponding_node():
    nodes = parse_legacy(
        "10001 first | 10002 second",
        images_by_node=[["https://example.com/1.png"], ["https://example.com/2.png"]],
    )

    assert nodes[0].content[-1].value.endswith("1.png")
    assert nodes[1].content[-1].value.endswith("2.png")


def test_legacy_accepts_textual_at_placeholder_as_sender_id():
    nodes = parse_legacy("[At:2116183730] 1")

    assert nodes[0].sender_id == "2116183730"
    assert [(item.type, item.value) for item in nodes[0].content] == [
        ("text", "1"),
    ]


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ('[{"sender_id":1,"content":[]}]', "content"),
        ('[{"sender_id":1,"content":[{"type":"image","url":"file:///a"}]}]', "HTTP/HTTPS"),
        ('[{"sender_id":1,"content":[{"type":"unknown"}]}]', "不支持"),
    ],
)
def test_invalid_json_nodes_are_rejected(value, message):
    with pytest.raises(ForwardMessageError, match=message):
        parse_json_nodes(value)


def test_limits_apply_before_payload_construction():
    with pytest.raises(ForwardMessageError, match="节点数量"):
        parse_dsl("1: a\n2: b", Limits(max_nodes=1))
    with pytest.raises(ForwardMessageError, match="总文本长度"):
        parse_dsl("1: abc", Limits(max_total_text_length=2))


def test_membership_resolution_and_references():
    nodes = parse_dsl("10001: hi[at:10002]")
    assert referenced_member_ids(nodes) == {"10001", "10002"}
    resolved = resolve_member_names(nodes, {"10001": "A", "10002": "B"})
    assert resolved[0].sender_name == "A"
    with pytest.raises(ForwardMessageError, match="10002"):
        resolve_member_names(nodes, {"10001": "A"})


def test_payload_uses_modern_napcat_fields_and_safe_previews():
    nodes = resolve_member_names(
        parse_dsl("10001: hello[at:10002]\n10002: [image:https://example.com/a.png]"),
        {"10001": "A", "10002": "B"},
    )
    payload = build_forward_payload(
        "90001",
        nodes,
        Presentation(prompt="P", summary="{count} records", source="S"),
    )

    assert payload["group_id"] == 90001
    assert payload["prompt"] == "P"
    assert payload["summary"] == "2 records"
    assert payload["source"] == "S"
    assert payload["news"] == [
        {"text": "A: hello@10002"},
        {"text": "B: [图片]"},
    ]
    assert payload["messages"][0]["data"]["content"][1] == {
        "type": "at",
        "data": {"qq": "10002"},
    }
