from app.models.channel import Channel
from app.repositories.base import BaseRepository


class ChannelsRepository(BaseRepository[Channel]):
    model = Channel
