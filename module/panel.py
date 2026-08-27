"""Shared helpers for persistent Discord panels."""

import asyncio

import discord


_PANEL_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}


def channel_send_capabilities(guild, channel) -> tuple[bool, bool]:
    """한 번의 권한 조회로 (패널 전송 가능, 첨부 공지 가능)을 반환한다."""
    if channel is None or getattr(channel, "type", None) not in {
        discord.ChannelType.text,
        discord.ChannelType.news,
    }:
        return False, False
    member = getattr(guild, "me", None)
    if member is None:
        return False, False
    permissions = channel.permissions_for(member)
    panel_allowed = all(
        getattr(permissions, name, False)
        for name in (
            "view_channel",
            "send_messages",
            "read_message_history",
            "embed_links",
        )
    )
    return panel_allowed, panel_allowed and bool(
        getattr(permissions, "attach_files", False)
    )


def is_sendable_panel_channel(guild, channel) -> bool:
    return channel_send_capabilities(guild, channel)[0]


def is_sendable_announcement_channel(guild, channel) -> bool:
    return channel_send_capabilities(guild, channel)[1]


def panel_lock(guild_id: int, panel_key: str) -> asyncio.Lock:
    return _PANEL_LOCKS.setdefault((guild_id, panel_key), asyncio.Lock())


def drop_panel_locks(guild_id: int) -> None:
    for lock_key in [
        lock_key for lock_key in _PANEL_LOCKS if lock_key[0] == guild_id
    ]:
        del _PANEL_LOCKS[lock_key]


async def upsert_panel(channel, message_id, *, embed, view) -> discord.Message:
    if message_id is not None:
        try:
            message = await channel.fetch_message(message_id)
            await message.edit(embed=embed, view=view)
            return message
        except discord.NotFound:
            pass
    return await channel.send(embed=embed, view=view)
