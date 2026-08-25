"""Read-only repository for conversations — mutation endpoints land in a later ticket."""

from app.models.conversation import Conversation
from app.repositories.base import BaseRepository


class ConversationsRepository(BaseRepository[Conversation]):
    model = Conversation
