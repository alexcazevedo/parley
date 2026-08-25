from app.models.agent import Agent
from app.repositories.base import BaseRepository


class AgentsRepository(BaseRepository[Agent]):
    model = Agent
