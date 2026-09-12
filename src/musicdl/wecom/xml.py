from defusedxml import ElementTree

from .models import WeComEnvelope, WeComMessage


class XmlLimitError(ValueError):
    pass


_LIMIT = 64 * 1024
_INNER_LIMIT = 32 * 1024


def _root(data: bytes, limit: int):
    if len(data) > limit:
        raise XmlLimitError("xml_too_large")
    try:
        root = ElementTree.fromstring(data)
    except Exception:
        raise XmlLimitError("invalid_xml") from None
    if root.tag != "xml":
        raise XmlLimitError("invalid_root")
    count = 0
    def walk(node, depth):
        nonlocal count
        count += 1
        if count > 64 or depth > 4:
            raise XmlLimitError("xml_limits_exceeded")
        for child in list(node):
            walk(child, depth + 1)
    walk(root, 0)
    return root


def _fields(root):
    result = {}
    for child in root:
        if child.tag in result:
            raise XmlLimitError("duplicate_field")
        result[child.tag] = child.text or ""
    return result


def parse_envelope(data: bytes) -> WeComEnvelope:
    fields = _fields(_root(data, _LIMIT))
    if not fields.get("Encrypt") or not fields.get("ToUserName") or not fields.get("AgentID"):
        raise XmlLimitError("missing_encrypt")
    if len(fields["Encrypt"]) > 64 * 1024:
        raise XmlLimitError("encrypt_too_large")
    return WeComEnvelope(fields["Encrypt"], fields.get("MsgSignature"), fields.get("TimeStamp"), fields.get("Nonce"))


def parse_message(data: bytes) -> WeComMessage:
    fields = _fields(_root(data, _INNER_LIMIT))
    if not fields.get("FromUserName") or not fields.get("MsgType") or not fields.get("CreateTime"):
        raise XmlLimitError("missing_message_field")
    if len(fields["FromUserName"]) > 128 or len(fields["MsgType"]) > 32 or len(fields.get("MsgId", "")) > 64 or len(fields.get("Content", "")) > 4096:
        raise XmlLimitError("field_too_large")
    agent = fields.get("AgentID")
    try:
        agent_id = int(agent) if agent else None
    except ValueError:
        raise XmlLimitError("invalid_agent_id") from None
    try:
        create_time = int(fields["CreateTime"])
    except ValueError:
        raise XmlLimitError("invalid_create_time") from None
    if fields["MsgType"] == "text" and (not fields.get("Content") or not fields.get("MsgId")):
        raise XmlLimitError("missing_text_field")
    return WeComMessage(fields["FromUserName"], fields.get("Content", ""), fields["MsgType"], agent_id, create_time, fields.get("MsgId"), fields.get("Event"))
