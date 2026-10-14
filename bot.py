"""Discord adapter. Uses public discord.py APIs, no user-account automation."""
import asyncio
import logging

import aiohttp
import discord

from commands import register_commands
from config import Settings
from recovery import Diagnosis, VoiceFailure, classify_failure, next_retry_delay
from voice_controller import Snapshot, VoiceController

log = logging.getLogger(__name__)


class ObservedVoiceClient(discord.VoiceClient):
    """Notify only after the SDK has processed voice state/server updates."""
    async def on_voice_state_update(self, data):
        try:
            await super().on_voice_state_update(data)
        finally:
            self.client.controller.notify()

    async def on_voice_server_update(self, data):
        try:
            await super().on_voice_server_update(data)
        finally:
            self.client.controller.notify()

    async def disconnect(self, *, force=False):
        try:
            await super().disconnect(force=force)
        finally:
            self.client.controller.notify()


class DiscordVoiceAdapter:
    def __init__(self, bot):
        self.bot = bot

    def _guild(self):
        return self.bot.get_guild(self.bot.settings.guild_id)

    def snapshot(self) -> Snapshot:
        guild = self._guild()
        ready = self.bot.gateway_available and not self.bot.is_closed()
        if guild is None:
            return Snapshot(ready=ready)
        vc = guild.voice_client
        state = guild.me.voice if guild.me is not None else None
        return Snapshot(
            ready=ready and not guild.unavailable,
            session_exists=vc is not None,
            connected=vc.is_connected() if vc else False,
            channel_id=state.channel.id if state and state.channel else None,
            muted_deafened=bool(state and state.self_mute and state.self_deaf),
        )

    def _target(self):
        guild = self._guild()
        if guild is None:
            raise VoiceFailure("configuration", "Bot is not in DISCORD_GUILD_ID; check invite and server ID.")
        if guild.unavailable or not self.bot.gateway_available:
            raise VoiceFailure("discord", "Gateway or configured server is unavailable; waiting for recovery.")
        channel = guild.get_channel(self.bot.settings.channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            raise VoiceFailure("configuration", "Target must be an existing regular voice channel in the configured server.")
        if guild.me is None:
            raise VoiceFailure("unknown", "Bot member state is not ready yet.")
        permissions = channel.permissions_for(guild.me)
        if not permissions.view_channel or not permissions.connect:
            raise VoiceFailure("configuration", "Bot needs View Channel and Connect in the target channel.")
        if guild.get_role(self.bot.settings.role_id) is None:
            raise VoiceFailure("configuration", "Configured control role does not exist in this server.")
        # No MOVE_MEMBERS privilege is required. Full channels must have a slot.
        already_here = bool(guild.me.voice and guild.me.voice.channel == channel)
        if (not already_here and channel.user_limit and len(channel.voice_states) >= channel.user_limit
                and not permissions.move_members):
            raise VoiceFailure("configuration", "Target channel is full; leave a slot or remove its user limit.")
        return guild, channel

    async def join(self) -> None:
        guild, channel = self._target()
        if guild.voice_client is not None:
            # State may have changed after the controller took its snapshot.
            return
        log.info("Joining configured voice channel id=%s", channel.id)
        try:
            await asyncio.wait_for(channel.connect(
                cls=ObservedVoiceClient, timeout=30.0, reconnect=True,
                self_mute=True, self_deaf=True), timeout=45.0)
        except BaseException:
            # A cancelled initial connect can otherwise leave a registered
            # voice client whose internal reconnect task survives /leave.
            await self.disconnect()
            raise

    async def move_back(self) -> None:
        guild, channel = self._target()
        vc = guild.voice_client
        if vc is None:
            await self.join()
            return
        log.info("Returning to configured voice channel id=%s", channel.id)
        await vc.move_to(channel, timeout=30.0)
        await self.ensure_silent()

    async def ensure_silent(self) -> None:
        guild, channel = self._target()
        await guild.change_voice_state(channel=channel, self_mute=True, self_deaf=True)

    async def disconnect(self) -> None:
        guild = self._guild()
        if guild is None:
            return
        vc = guild.voice_client
        if vc is not None:
            await vc.disconnect(force=True)
        elif (self.bot.gateway_available and guild.me is not None
              and guild.me.voice is not None and guild.me.voice.channel is not None):
            # Also clear a server-side voice state left behind by a failed join.
            await guild.change_voice_state(channel=None)

    async def diagnose(self) -> Diagnosis:
        """Only called after a sustained stall, never to judge healthy voice.

        Two small public HTTPS checks provide evidence, not proof of cause.
        Neither request carries a bot token or information about server users.
        """
        session = self.bot.diagnostic_http
        if session is None or session.closed:
            return Diagnosis("unknown", "Diagnostic HTTP session unavailable.", True)

        async def fetch(url):
            try:
                async with session.get(url) as response:
                    if response.status != 200:
                        return response.status, None
                    return response.status, await response.json()
            except (aiohttp.ClientError, OSError, TimeoutError) as error:
                return None, error

        status, gateway = await asyncio.gather(
            fetch("https://discordstatus.com/api/v2/summary.json"),
            fetch("https://discord.com/api/v10/gateway"),
        )
        if status[0] == 200 and isinstance(status[1], dict):
            components = status[1].get("components", [])
            relevant = [item for item in components if isinstance(item, dict)
                        and any(term in item.get("name", "").lower() for term in ("voice", "gateway", "api"))]
            if any(item.get("status") not in {None, "operational"} for item in relevant):
                return Diagnosis("discord", "Discord reports a voice/API/gateway incident; leaving session recovery to SDK.", True)
        if gateway[0] is not None and gateway[0] >= 500:
            return Diagnosis("discord", "Discord gateway API returned a server error.", True)
        if gateway[0] == 429:
            return Diagnosis("rate_limit", "Public gateway probe was rate limited; preserving session.", True)
        if gateway[0] is None:
            return Diagnosis("network", "Gateway probe unreachable; hosting/network or Discord may be responsible.", True)
        return Diagnosis("unknown", "Gateway HTTPS reachable; no confirmed relevant incident. This does not prove voice/UDP health.")


class VoiceKeeperBot(discord.Client):
    def __init__(self, settings: Settings):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents, max_messages=None,
                         member_cache_flags=discord.MemberCacheFlags.none(),
                         allowed_mentions=discord.AllowedMentions.none())
        self.settings = settings
        self.gateway_available = False
        self.diagnostic_http = None
        self._sync_task = None
        self.adapter = DiscordVoiceAdapter(self)
        self.controller = VoiceController(self.adapter, settings.channel_id)
        self.tree = register_commands(self)

    async def setup_hook(self):
        self.diagnostic_http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
        # Run once per login, not every on_ready event. Sync failures must not
        # prevent an otherwise valid voice connection from starting.
        self._sync_task = asyncio.create_task(self._sync_commands(), name="command-sync")

    async def _sync_commands(self):
        await self.wait_until_ready()
        attempt = 0
        while not self.is_closed():
            try:
                await self.tree.sync(guild=discord.Object(id=self.settings.guild_id))
                log.info("Registered /join and /leave in configured server.")
                return
            except (discord.HTTPException, aiohttp.ClientError, OSError) as error:
                diagnosis = classify_failure(error)
                attempt += 1
                delay = max(30.0, next_retry_delay(attempt, diagnosis.category))
                log.warning("command_sync_failed cause=%s retry_in=%.1fs detail=%s",
                            diagnosis.category, delay, diagnosis.detail)
                await asyncio.sleep(delay)

    async def on_ready(self):
        self.gateway_available = True
        log.info("Logged in as: %s (ID: %s)", self.user, self.user.id)
        log.info("Configured server ID: %s", self.settings.guild_id)
        log.info("Visible servers: %s",
                 [(g.name, g.id) for g in self.guilds])
        self.controller.notify()

    async def on_resumed(self):
        self.gateway_available = True
        log.info("Discord gateway resumed; preserving requested voice state.")
        self.controller.notify()

    async def on_disconnect(self):
        if self.gateway_available:
            log.warning("Gateway disconnected; cause unknown. Existing voice is not forcibly disconnected.")
        self.gateway_available = False
        self.controller.notify()

    async def on_voice_state_update(self, member, before, after):
        if self.user and member.id == self.user.id and member.guild.id == self.settings.guild_id:
            if before.channel != after.channel:
                log.info("Bot voice channel changed: before=%s after=%s",
                         before.channel.id if before.channel else None,
                         after.channel.id if after.channel else None)
            self.controller.notify()

    async def on_guild_available(self, guild):
        if guild.id == self.settings.guild_id:
            self.controller.notify()

    async def on_guild_unavailable(self, guild):
        if guild.id == self.settings.guild_id:
            log.warning("Configured server unavailable; preserving voice session.")
            self.controller.notify()

    async def close(self):
        self.controller.request_shutdown()
        if self._sync_task:
            self._sync_task.cancel()
            await asyncio.gather(self._sync_task, return_exceptions=True)
        try:
            await super().close()
        finally:
            if self.diagnostic_http is not None:
                await self.diagnostic_http.close()
