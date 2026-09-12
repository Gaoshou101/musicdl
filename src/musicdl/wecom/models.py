from dataclasses import dataclass


@dataclass(frozen=True)
class WeComEnvelope:
    encrypt: str
    msg_signature: str | None = None
    timestamp: str | None = None
    nonce: str | None = None


@dataclass(frozen=True)
class WeComMessage:
    from_user: str
    content: str = ""
    msg_type: str = ""
    agent_id: int | None = None
    create_time: int = 0
    msg_id: str | None = None
    event: str | None = None
