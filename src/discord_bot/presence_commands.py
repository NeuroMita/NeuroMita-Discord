from __future__ import annotations

import discord
from discord import app_commands

def register_presence_commands(bot) -> None:

    presence_group = app_commands.Group(name="presence", description="Ambient room participation")

    @presence_group.command(name="status", description="Show room presence and anti-spam state")
    async def presence_status(interaction: discord.Interaction) -> None:
        if not await bot._require_admin(interaction):
            return
        if bot.presence is None:
            await interaction.response.send_message("Room presence runtime is unavailable.", ephemeral=True)
            return
        state = bot.presence.status()
        await interaction.response.send_message(
            f"Mode: {state['mode']} | enabled: {state['enabled']} | initiative: {state['initiative']}\n"
            f"Voluntary: {state['voluntary_1h_count']} / {state['max_per_hour']} this hour\n"
            f"Autonomous: {state['initiative_6h_count']} / {state['initiative_max_per_6h']} this 6h\n"
            f"Unanswered streak: {state['unanswered_streak']} | cooldown: {state['cooldown_seconds']}s\n"
            f"Last initiative: {state['last_initiative_at'] or 'never'}\n"
            f"Last silence ping: {state['last_silence_ping_at'] or 'never'}\n"
            f"Messages seen: {state['messages_seen']} | last: "
            f"{state['last_attention_action']} ({state['last_attention_reason']})\n"
            f"Paused until: {state['paused_until'] or 'no'} | generation busy: {state['generation_busy']}\n"
            f"Utility busy: {state['utility_busy']} | attention busy: {state['attention_busy']} | "
            f"observation busy: {state['observation_busy']}",
            ephemeral=True,
        )

    @presence_group.command(name="on", description="Enable ambient room presence")
    async def presence_on(interaction: discord.Interaction) -> None:
        if not await bot._require_admin(interaction):
            return
        if bot.presence_settings is None:
            await interaction.response.send_message("Room presence runtime is unavailable.", ephemeral=True)
            return
        bot.presence_settings.update(enabled=True)
        await interaction.response.send_message("Ambient presence enabled.", ephemeral=True)

    @presence_group.command(name="off", description="Disable ambient room presence")
    async def presence_off(interaction: discord.Interaction) -> None:
        if not await bot._require_admin(interaction):
            return
        if bot.presence_settings is None:
            await interaction.response.send_message("Room presence runtime is unavailable.", ephemeral=True)
            return
        bot.presence_settings.update(enabled=False)
        await interaction.response.send_message("Ambient presence disabled; direct replies still work.", ephemeral=True)

    @presence_group.command(name="mode", description="Choose direct, social, or alive mode")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Direct", value="direct"),
        app_commands.Choice(name="Social", value="social"),
        app_commands.Choice(name="Alive", value="alive"),
    ])
    async def presence_mode(interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        if not await bot._require_admin(interaction):
            return
        bot.presence_settings.update(mode=mode.value)
        await interaction.response.send_message(f"Presence mode set to {mode.value}.", ephemeral=True)

    @presence_group.command(name="initiative", description="Set how readily Mita joins in (0-100)")
    async def presence_initiative(interaction: discord.Interaction, value: app_commands.Range[int, 0, 100]) -> None:
        if not await bot._require_admin(interaction):
            return
        bot.presence_settings.update(initiative=value)
        await interaction.response.send_message(f"Initiative set to {value}.", ephemeral=True)

    @presence_group.command(name="cooldown", description="Set minimum voluntary message interval")
    async def presence_cooldown(interaction: discord.Interaction, seconds: app_commands.Range[int, 30, 86400]) -> None:
        if not await bot._require_admin(interaction):
            return
        bot.presence_settings.update(cooldown_seconds=seconds)
        await interaction.response.send_message(f"Voluntary cooldown set to {seconds}s.", ephemeral=True)

    @presence_group.command(name="max-hour", description="Set the voluntary message cap per hour")
    async def presence_max_hour(interaction: discord.Interaction, count: app_commands.Range[int, 0, 20]) -> None:
        if not await bot._require_admin(interaction):
            return
        bot.presence_settings.update(max_per_hour=count)
        await interaction.response.send_message(f"Voluntary hourly cap set to {count}.", ephemeral=True)

    @presence_group.command(name="pause", description="Pause ambient presence for up to 24 hours")
    async def presence_pause(interaction: discord.Interaction, minutes: app_commands.Range[int, 1, 1440]) -> None:
        if not await bot._require_admin(interaction):
            return
        bot.presence.set_pause(minutes)
        await interaction.response.send_message(f"Ambient presence paused for {minutes} minutes.", ephemeral=True)

    @presence_group.command(name="resume", description="Resume ambient presence")
    async def presence_resume(interaction: discord.Interaction) -> None:
        if not await bot._require_admin(interaction):
            return
        bot.presence.set_pause(None)
        await interaction.response.send_message("Ambient presence resumed.", ephemeral=True)

    @presence_group.command(name="speak-now", description="Ask Mita to make one voluntary room message")
    async def presence_speak_now(interaction: discord.Interaction) -> None:
        if not await bot._require_admin(interaction):
            return
        if bot.presence is None:
            await interaction.response.send_message("Room presence runtime is unavailable.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        sent = await bot.presence.speak_now()
        await interaction.edit_original_response(
            content="Mita posted a message." if sent else "No message was sent; check /presence status and its gates."
        )

    bot.tree.add_command(presence_group)

    room_group = app_commands.Group(name="room", description="Observed Discord room context")

    @room_group.command(name="status", description="Show room timeline and summary size")
    async def room_status(interaction: discord.Interaction) -> None:
        if not await bot._require_admin(interaction):
            return
        count = bot.room_timeline.count_messages(
            guild_id=str(bot.config.guild_id), channel_id=str(bot.config.channel_id),
        )
        summary = bot.room_timeline.get_summary(
            guild_id=str(bot.config.guild_id), channel_id=str(bot.config.channel_id),
        )
        await interaction.response.send_message(
            f"Timeline messages: {count} | room summary: {len(summary)} chars | DB: {bot.room_timeline.path}",
            ephemeral=True,
        )

    @room_group.command(name="recent", description="Show recent observed room messages")
    async def room_recent(interaction: discord.Interaction, limit: app_commands.Range[int, 1, 10] = 5) -> None:
        if not await bot._require_admin(interaction):
            return
        messages = bot.room_timeline.recent(
            guild_id=str(bot.config.guild_id), channel_id=str(bot.config.channel_id), limit=limit,
        )
        lines = [f"{m.author_name} [id:{m.author_id}]: {m.content[:220]}" for m in messages]
        await interaction.response.send_message(
            "\n".join(lines)[-1900:] or "Room timeline is empty.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )

    @room_group.command(name="summary", description="Show the room summary used in context")
    async def room_summary(interaction: discord.Interaction) -> None:
        if not await bot._require_admin(interaction):
            return
        summary = bot.room_timeline.get_summary(
            guild_id=str(bot.config.guild_id), channel_id=str(bot.config.channel_id),
        )
        await interaction.response.send_message(
            summary[:1900] or "No room summary yet.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )

    @room_group.command(name="summarize", description="Manually update the room summary")
    async def room_summarize(interaction: discord.Interaction) -> None:
        if not await bot._require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        completed = await bot.presence.summarize_room()
        await interaction.edit_original_response(
            content="Room summary updated." if completed else "No summary update was produced; check /debug last-error."
        )

    @room_group.command(name="reset", description="Clear only the Discord room timeline and summary")
    @app_commands.describe(confirm="Confirm deleting only Discord room context")
    async def room_reset(interaction: discord.Interaction, confirm: bool) -> None:
        if not await bot._require_admin(interaction):
            return
        if not confirm:
            await interaction.response.send_message("Set confirm=true to clear Discord room context only.", ephemeral=True)
            return
        removed = await bot.clear_room_context()
        await interaction.response.send_message(
            f"Cleared {removed} room messages and summary. Character memory/history were not changed.",
            ephemeral=True,
        )

    bot.tree.add_command(room_group)

    attention_group = app_commands.Group(name="attention", description="Test room attention decisions")

    @attention_group.command(name="test", description="Evaluate the current room without sending a message")
    async def attention_test(interaction: discord.Interaction) -> None:
        if not await bot._require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await bot.presence.test_attention()
        await interaction.edit_original_response(
            content=(
                f"speak: {result.speak} | action: {result.action} | desire: {result.desire} | "
                f"reason: {result.reason_code} | diagnostic: {result.diagnostic_code or 'none'}"
                + (f" | status: {result.diagnostic_status}" if result.diagnostic_status else "")
            )
        )

    bot.tree.add_command(attention_group)
