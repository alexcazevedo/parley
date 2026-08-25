"""Maps `Channel.type` to its `ChannelAdapter` implementation."""

from app.channels.base import ChannelAdapter
from app.channels.webchat import WebChatAdapter
from app.models.channel import ChannelType

_ADAPTERS: dict[ChannelType, type[ChannelAdapter]] = {
    ChannelType.WEBCHAT: WebChatAdapter,
}


def get_adapter(channel_type: ChannelType) -> ChannelAdapter:
    adapter_cls = _ADAPTERS.get(channel_type)
    if adapter_cls is None:
        raise ValueError(f"no adapter registered for channel type {channel_type!r}")
    return adapter_cls()
