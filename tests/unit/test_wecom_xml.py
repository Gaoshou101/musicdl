import pytest

from musicdl.wecom.xml import XmlLimitError, parse_envelope, parse_message


def test_parses_outer_and_inner_messages():
    outer = b'<xml><ToUserName><![CDATA[corp]]></ToUserName><AgentID>7</AgentID><Encrypt><![CDATA[cipher]]></Encrypt><MsgSignature><![CDATA[sig]]></MsgSignature><TimeStamp>1</TimeStamp><Nonce><![CDATA[n]]></Nonce></xml>'
    assert parse_envelope(outer).encrypt == "cipher"
    inner = b'<xml><ToUserName><![CDATA[corp]]></ToUserName><FromUserName><![CDATA[alice]]></FromUserName><CreateTime>1</CreateTime><MsgType>text</MsgType><Content><![CDATA[/search hello]]></Content><MsgId>9</MsgId><AgentID>7</AgentID></xml>'
    message = parse_message(inner)
    assert message.from_user == "alice" and message.content == "/search hello"


def test_outer_requires_xml_root_and_required_unique_fields():
    with pytest.raises(XmlLimitError):
        parse_envelope(b'<bad><ToUserName>x</ToUserName><AgentID>7</AgentID><Encrypt>c</Encrypt></bad>')
    with pytest.raises(XmlLimitError):
        parse_envelope(b'<xml><ToUserName>x</ToUserName><AgentID>7</AgentID><Encrypt></Encrypt></xml>')


def test_inner_exposes_create_time_and_msg_id():
    data = b'<xml><FromUserName>alice</FromUserName><CreateTime>12</CreateTime><MsgType>text</MsgType><Content>x</Content><MsgId>m</MsgId></xml>'
    message = parse_message(data)
    assert message.create_time == 12 and message.msg_id == "m"


def test_text_requires_content_and_msg_id_but_event_may_omit_msg_id():
    missing = b'<xml><FromUserName>a</FromUserName><CreateTime>1</CreateTime><MsgType>text</MsgType></xml>'
    with pytest.raises(XmlLimitError):
        parse_message(missing)
    event = b'<xml><FromUserName>a</FromUserName><CreateTime>1</CreateTime><MsgType>event</MsgType><Event>click</Event></xml>'
    assert parse_message(event).event == "click"


def test_inner_field_lengths_are_bounded():
    data = b'<xml><FromUserName>' + b'a' * 129 + b'</FromUserName><CreateTime>1</CreateTime><MsgType>text</MsgType><Content>x</Content><MsgId>m</MsgId></xml>'
    with pytest.raises(XmlLimitError):
        parse_message(data)


@pytest.mark.parametrize("xml", [b'<!DOCTYPE xml [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><xml>&xxe;</xml>', b'<xml><Encrypt>a</Encrypt><Encrypt>b</Encrypt></xml>'])
def test_rejects_entities_and_duplicate_fields(xml):
    with pytest.raises(XmlLimitError):
        parse_envelope(xml)


def test_rejects_depth_element_and_size_limits():
    with pytest.raises(XmlLimitError):
        parse_envelope(b'<xml><a><b><c><d><e/></d></c></b></a></xml>')
    with pytest.raises(XmlLimitError):
        parse_envelope(b'<xml>' + b''.join(b'<x/>' for _ in range(65)) + b'</xml>')
    with pytest.raises(XmlLimitError):
        parse_envelope(b'<xml><Encrypt>' + b'x' * (64 * 1024) + b'</Encrypt></xml>')
