"""Only two guild commands; authorization is checked on every invocation."""
from typing import Iterable
from config import Settings


def is_allowed(guild_id: int | None, role_ids: Iterable[int], settings: Settings) -> bool:
    return guild_id == settings.guild_id and settings.role_id in role_ids


def register_commands(bot):
    import discord
    from discord import app_commands
    import logging

    class RoleCheckedTree(app_commands.CommandTree):
        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            member = interaction.user
            roles = [role.id for role in member.roles] if isinstance(member, discord.Member) else []
            if is_allowed(interaction.guild_id, roles, bot.settings):
                return True
            await interaction.response.send_message(
                "You need the configured control role in this server to use this command.", ephemeral=True)
            return False

        async def on_error(self, interaction, error):
            logging.getLogger(__name__).error("command_error=%s", type(error).__name__)
            text = "The command could not complete. Check the hosting logs."
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)

    tree = RoleCheckedTree(bot)
    guild = discord.Object(id=bot.settings.guild_id)

    @tree.command(name="join", description="Join the fixed voice channel and enable automatic recovery.", guild=guild)
    @app_commands.guild_only()
    async def join(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot.controller.request_join()
        await interaction.followup.send(
            "Automatic joining is enabled. I'll connect to the configured channel, or stay if already there.",
            ephemeral=True)

    @tree.command(name="leave", description="Leave voice until /join or the next process restart.", guild=guild)
    @app_commands.guild_only()
    async def leave(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot.controller.request_leave()
        await interaction.followup.send(
            "Automatic joining is paused and disconnection has been requested. "
            "Use /join to return. A hosting restart also enables joining again.", ephemeral=True)

    return tree
