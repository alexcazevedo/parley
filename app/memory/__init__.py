"""Long-term memory: storage, retrieval and the backing service for the
`remember` built-in tool (see app/tools/builtin.py) and the `/contacts/{id}/memories`
REST CRUD (see app/api/contacts.py).
"""

from app.memory.service import MemoryService

__all__ = ["MemoryService"]
