"""Queue abstraction and the `Job` payload it moves between the API process
(enqueue) and the worker process (dequeue/ack/fail).
"""

import abc
import json
import uuid
from dataclasses import dataclass


@dataclass
class Job:
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    attempt: int = 0

    def to_json(self) -> str:
        return json.dumps(
            {
                "conversation_id": str(self.conversation_id),
                "message_id": str(self.message_id),
                "attempt": self.attempt,
            }
        )

    @classmethod
    def from_json(cls, data: str) -> "Job":
        payload = json.loads(data)
        return cls(
            conversation_id=uuid.UUID(payload["conversation_id"]),
            message_id=uuid.UUID(payload["message_id"]),
            attempt=payload.get("attempt", 0),
        )


class Queue(abc.ABC):
    @abc.abstractmethod
    async def enqueue(self, job: Job) -> None: ...

    @abc.abstractmethod
    async def dequeue(self, timeout: int = 0) -> Job | None: ...

    @abc.abstractmethod
    async def ack(self, job: Job) -> None: ...

    @abc.abstractmethod
    async def fail(self, job: Job) -> None: ...
