"""Shared helpers for persistent Discord panels."""

import asyncio

import discord


_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}


def is_sendable_panel_channel(guild, channel) -> bool:
    if channel is None or getattr(channel, "type", None) not in {
        discord.ChannelType.text,
        discord.ChannelType.news,
    }:
        return False
    member = getattr(guild, "me", None)
    if member is None:
        return False
    permissions = channel.permissions_for(member)
    return all(
        getattr(permissions, name, False)
        for name in (
            "view_channel",
            "send_messages",
            "read_message_history",
            "embed_links",
        )
    )


def is_sendable_announcement_channel(guild, channel) -> bool:
    if not is_sendable_panel_channel(guild, channel):
        return False
    return bool(getattr(channel.permissions_for(guild.me), "attach_files", False))


def panel_lock(guild_id: int, panel_key: str) -> asyncio.Lock:
    return _LOCKS.setdefault((guild_id, panel_key), asyncio.Lock())


def drop_panel_locks(guild_id: int) -> None:
    for key in [key for key in _LOCKS if key[0] == guild_id]:
        del _LOCKS[key]


async def upsert_panel(channel, message_id, *, embed, view) -> discord.Message:
    if message_id is not None:
        try:
            message = await channel.fetch_message(message_id)
            await message.edit(embed=embed, view=view)
            return message
        except discord.NotFound:
            pass
    return await channel.send(embed=embed, view=view)
