"""Test the bootstrap endpoint for the demo webchat."""

import uuid
from sqlalchemy import select

from app.models.channel import Channel


async def test_bootstrap_creates_channel_with_humanize(client, db_session):
    """Bootstrap creates a new channel with humanize: true."""
    response = await client.post("/chat/bootstrap")
    assert response.status_code == 200
    data = response.json()

    # Verify response contains channel_id
    assert "channel_id" in data
    channel_id = uuid.UUID(data["channel_id"])

    # Query the channel from the database
    result = await db_session.execute(
        select(Channel).where(Channel.id == channel_id)
    )
    channel = result.scalar_one()

    # Verify humanize is in config and is True
    assert "humanize" in channel.config
    assert channel.config["humanize"] is True


async def test_bootstrap_idempotent_upgrade_humanize(client, db_session):
    """Bootstrap upgrades existing channel's config if humanize is missing."""
    # First call creates the channel
    response1 = await client.post("/chat/bootstrap")
    assert response1.status_code == 200
    channel_id1 = response1.json()["channel_id"]

    # Manually set config to empty (simulating pre-existing channel)
    result = await db_session.execute(
        select(Channel).where(Channel.id == channel_id1)
    )
    channel = result.scalar_one()
    channel.config = {}
    await db_session.commit()

    # Second call should upgrade the config
    response2 = await client.post("/chat/bootstrap")
    assert response2.status_code == 200
    data = response2.json()

    # Should return the same channel
    assert data["channel_id"] == channel_id1

    # Verify config was upgraded
    await db_session.refresh(channel)
    assert "humanize" in channel.config
    assert channel.config["humanize"] is True
