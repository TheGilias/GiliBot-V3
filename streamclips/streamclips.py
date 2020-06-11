import discord
from redbot.core.bot import Red
from redbot.core import checks, commands, Config
from redbot.core.utils._internal_utils import send_to_owners_with_prefix_replaced
from redbot.core.utils.chat_formatting import escape, pagify

from .streamtypes import (
    HitboxStream,
    MixerStream,
    PicartoStream,
    Stream,
    TwitchStream,
    YoutubeStream,
)
from .errors import (
    APIError,
    InvalidTwitchCredentials,
    InvalidYoutubeCredentials,
    StreamNotFound,
    StreamsError,
)
from . import streamtypes as _streamtypes

import re
import logging
import asyncio
import aiohttp
import contextlib
from datetime import datetime
from collections import defaultdict
from typing import Optional, List, Tuple, Union

log = logging.getLogger("red.core.cogs.StreamClips")

class StreamClips(commands.Cog):
    """This cog is designed to put alerts in a text channel when new clips from specified streamers are detected."""

    global_defaults = {"refresh_timer": 300, "tokens": {}, "streams": []}

    guild_defaults = {
        "autodelete": False,
        "mention_everyone": False,
        "mention_here": False,
        "live_message_mention": False,
        "live_message_nomention": False,
        "ignore_reruns": False,
    }

    role_defaults = {"mention": False}

    def __init__(self, bot: Red):
        super().__init__()
        self.config: Config = Config.get_conf(self, 84761239)
        self.ttv_bearer_cache: dict = {}
        self.config.register_global(**self.global_defaults)
        self.config.register_guild(**self.guild_defaults)
        self.config.register_role(**self.role_defaults)

        self.bot: Red = bot

        self.streams: List[Stream] = []
        self.task: Optional[asyncio.Task] = None

        self.yt_cid_pattern = re.compile("^UC[-_A-Za-z0-9]{21}[AQgw]$")

        self._ready_event: asyncio.Event = asyncio.Event()
        self._init_task: asyncio.Task = self.bot.loop.create_task(self.initialize())

    def check_name_or_id(self, data: str) -> bool:
        matched = self.yt_cid_pattern.fullmatch(data)
        if matched is None:
            return True
        return False

    async def initialize(self) -> None:
        """Should be called straight after cog instantiation."""
        await self.bot.wait_until_ready()

        try:
            await self.move_api_keys()
            await self.get_twitch_bearer_token()
            self.streams = await self.load_streams()
            self.task = self.bot.loop.create_task(self._clip_alerts())
        except Exception as error:
            log.exception("Failed to initialize StreamClips cog:", exc_info=error)

        self._ready_event.set()

    async def cog_before_invoke(self, ctx: commands.Context):
        await self._ready_event.wait()

    async def move_api_keys(self) -> None:
        """Move the API keys from cog stored config to core bot config if they exist."""
        tokens = await self.config.tokens()
        youtube = await self.bot.get_shared_api_tokens("youtube")
        twitch = await self.bot.get_shared_api_tokens("twitch")
        for token_type, token in tokens.items():
            if token_type == "YoutubeStream" and "api_key" not in youtube:
                await self.bot.set_shared_api_tokens("youtube", api_key=token)
            if token_type == "TwitchStream" and "client_id" not in twitch:
                # Don't need to check Community since they're set the same
                await self.bot.set_shared_api_tokens("twitch", client_id=token)
        await self.config.tokens.clear()

    async def get_twitch_bearer_token(self) -> None:
        tokens = await self.bot.get_shared_api_tokens("twitch")
        if tokens.get("client_id"):
            try:
                tokens["client_secret"]
            except KeyError:
                message = _(
                    "You need a client secret key to use correctly Twitch API on this cog.\n"
                    "Follow these steps:\n"
                    "1. Go to this page: https://dev.twitch.tv/console/apps.\n"
                    '2. Click "Manage" on your application.\n'
                    '3. Click on "New secret".\n'
                    "5. Copy your client ID and your client secret into:\n"
                    "`[p]set api twitch client_id <your_client_id_here> "
                    "client_secret <your_client_secret_here>`\n\n"
                    "Note: These tokens are sensitive and should only be used in a private channel "
                    "or in DM with the bot."
                )
                await send_to_owners_with_prefix_replaced(self.bot, message)
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://id.twitch.tv/oauth2/token",
                params={
                    "client_id": tokens.get("client_id", ""),
                    "client_secret": tokens.get("client_secret", ""),
                    "grant_type": "client_credentials",
                },
            ) as req:
                try:
                    data = await req.json()
                except aiohttp.ContentTypeError:
                    data = {}

                if req.status == 200:
                    pass
                elif req.status == 400 and data.get("message") == "invalid client":
                    log.error(
                        "Twitch API request failed authentication: set Client ID is invalid."
                    )
                elif req.status == 403 and data.get("message") == "invalid client secret":
                    log.error(
                        "Twitch API request failed authentication: set Client Secret is invalid."
                    )
                elif "message" in data:
                    log.error(
                        "Twitch OAuth2 API request failed with status code %s"
                        " and error message: %s",
                        req.status,
                        data["message"],
                    )
                else:
                    log.error("Twitch OAuth2 API request failed with status code %s", req.status)

                if req.status != 200:
                    return

        self.ttv_bearer_cache = data
        self.ttv_bearer_cache["expires_at"] = datetime.now().timestamp() + data.get("expires_in")

    async def maybe_renew_twitch_bearer_token(self) -> None:
        if self.ttv_bearer_cache:
            if self.ttv_bearer_cache["expires_at"] - datetime.now().timestamp() <= 60:
                await self.get_twitch_bearer_token()

    @commands.group()
    @commands.guild_only()
    @checks.mod()
    async def clipalert(self, ctx: commands.Context):
        """Manage automated stream clip alerts."""
        pass

    @clipalert.command(name="mixer")
    async def mixer_clip_alert(self, ctx: commands.Context, channel_name: str):
        """Toggle alerts in this channel for a Mixer channel's clips."""
        await self.stream_clip_alert(ctx, MixerStream, channel_name)

    async def stream_clip_alert(self, ctx: commands.Context, _class, channel_name):
        stream = self.get_stream(_class, channel_name)
        if not stream:
            token = await self.bot.get_shared_api_tokens(_class.token_name)
            is_yt = _class.__name__ == "YoutubeStream"
            is_twitch = _class.__name__ == "TwitchStream"
            if is_yt and not self.check_name_or_id(channel_name):
                stream = _class(id=channel_name, token=token)
            elif is_twitch:
                await self.maybe_renew_twitch_bearer_token()
                stream = _class(
                    name=channel_name,
                    token=token.get("client_id"),
                    bearer=self.ttv_bearer_cache.get("access_token", None),
                )
            else:
                stream = _class(name=channel_name, token=token)
            try:
                exists = await self.check_exists(stream)
            except InvalidTwitchCredentials:
                await ctx.send(
                    _(
                        "The Twitch token is either invalid or has not been set. See "
                        "`{prefix}clipset twitchtoken`."
                    ).format(prefix=ctx.clean_prefix)
                )
                return
            except InvalidYoutubeCredentials:
                await ctx.send(
                    _(
                        "The YouTube API key is either invalid or has not been set. See "
                        "`{prefix}clipset youtubekey`."
                    ).format(prefix=ctx.clean_prefix)
                )
                return
            except APIError:
                await ctx.send(
                    _("Something went wrong whilst trying to contact the stream service's API.")
                )
                return
            else:
                if not exists:
                    await ctx.send(_("That channel doesn't seem to exist."))
                    return

        await self.add_or_remove(ctx, stream)

    async def add_or_remove(self, ctx: commands.Context, stream):
        if ctx.channel.id not in stream.channels:
            stream.channels.append(ctx.channel.id)
            if stream not in self.streams:
                self.streams.append(stream)
            await ctx.send(
                _(
                    "I'll now send a notification in this channel when {stream.name} has new clips."
                ).format(stream=stream)
            )
        else:
            stream.channels.remove(ctx.channel.id)
            if not stream.channels:
                self.streams.remove(stream)
            await ctx.send(
                _(
                    "I won't send notifications about {stream.name}'s clips in this channel anymore."
                ).format(stream=stream)
            )

        await self.save_streams()

    def get_stream(self, _class, name):
        for stream in self.streams:
            # if isinstance(stream, _class) and stream.name == name:
            #    return stream
            # Reloading this cog causes an issue with this check ^
            # isinstance will always return False
            # As a workaround, we'll compare the class' name instead.
            # Good enough.
            if _class.__name__ == "YoutubeStream" and stream.type == _class.__name__:
                # Because name could be a username or a channel id
                if self.check_name_or_id(name) and stream.name.lower() == name.lower():
                    return stream
                elif not self.check_name_or_id(name) and stream.id == name:
                    return stream
            elif stream.type == _class.__name__ and stream.name.lower() == name.lower():
                return stream

    @staticmethod
    async def check_exists(stream):
        try:
            await stream.is_online()
        except OfflineStream:
            pass
        except StreamNotFound:
            return False
        except StreamsError:
            raise
        return True

    async def _clip_alerts(self):
        await self.bot.wait_until_ready()
        while True:
            try:
                await self.check_clips()
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(await self.config.refresh_timer())

    async def check_clips(self):
        """for stream in self.streams:
            with contextlib.suppress(Exception):
                try:
                    if stream.__class__.__name__ == "TwitchStream":
                        await self.maybe_renew_twitch_bearer_token()
                        embed, is_rerun = await stream.is_online()
                    else:
                        embed = await stream.is_online()
                        is_rerun = False
                except OfflineStream:
                    if not stream._messages_cache:
                        continue
                    for message in stream._messages_cache:
                        with contextlib.suppress(Exception):
                            autodelete = await self.config.guild(message.guild).autodelete()
                            if autodelete:
                                await message.delete()
                    stream._messages_cache.clear()
                    await self.save_streams()
                else:
                    if stream._messages_cache:
                        continue
                    for channel_id in stream.channels:
                        channel = self.bot.get_channel(channel_id)
                        if not channel:
                            continue
                        ignore_reruns = await self.config.guild(channel.guild).ignore_reruns()
                        if ignore_reruns and is_rerun:
                            continue
                        mention_str, edited_roles = await self._get_mention_str(channel.guild)

                        if mention_str:
                            alert_msg = await self.config.guild(
                                channel.guild
                            ).live_message_mention()
                            if alert_msg:
                                content = alert_msg.format(mention=mention_str, stream=stream)
                            else:
                                content = _("{mention}, {stream} is live!").format(
                                    mention=mention_str,
                                    stream=escape(
                                        str(stream.name), mass_mentions=True, formatting=True
                                    ),
                                )
                        else:
                            alert_msg = await self.config.guild(
                                channel.guild
                            ).live_message_nomention()
                            if alert_msg:
                                content = alert_msg.format(stream=stream)
                            else:
                                content = _("{stream} is live!").format(
                                    stream=escape(
                                        str(stream.name), mass_mentions=True, formatting=True
                                    )
                                )

                        m = await channel.send(content, embed=embed)
                        stream._messages_cache.append(m)
                        if edited_roles:
                            for role in edited_roles:
                                await role.edit(mentionable=False)
                        await self.save_streams()"""

    async def _get_mention_str(self, guild: discord.Guild) -> Tuple[str, List[discord.Role]]:
        """Returns a 2-tuple with the string containing the mentions, and a list of
        all roles which need to have their `mentionable` property set back to False.
        """
        settings = self.config.guild(guild)
        mentions = []
        edited_roles = []
        if await settings.mention_everyone():
            mentions.append("@everyone")
        if await settings.mention_here():
            mentions.append("@here")
        can_manage_roles = guild.me.guild_permissions.manage_roles
        for role in guild.roles:
            if await self.config.role(role).mention():
                if can_manage_roles and not role.mentionable:
                    try:
                        await role.edit(mentionable=True)
                    except discord.Forbidden:
                        # Might still be unable to edit role based on hierarchy
                        pass
                    else:
                        edited_roles.append(role)
                mentions.append(role.mention)
        return " ".join(mentions), edited_roles

    async def filter_streams(self, streams: list, channel: discord.TextChannel) -> list:
        filtered = []
        for stream in streams:
            tw_id = str(stream["channel"]["_id"])
            for alert in self.streams:
                if isinstance(alert, TwitchStream) and alert.id == tw_id:
                    if channel.id in alert.channels:
                        break
            else:
                filtered.append(stream)
        return filtered

    async def load_streams(self):
        streams = []
        for raw_stream in await self.config.streams():
            _class = getattr(_streamtypes, raw_stream["type"], None)
            if not _class:
                continue
            raw_msg_cache = raw_stream["messages"]
            raw_stream["_messages_cache"] = []
            for raw_msg in raw_msg_cache:
                chn = self.bot.get_channel(raw_msg["channel"])
                if chn is not None:
                    try:
                        msg = await chn.fetch_message(raw_msg["message"])
                    except discord.HTTPException:
                        pass
                    else:
                        raw_stream["_messages_cache"].append(msg)
            token = await self.bot.get_shared_api_tokens(_class.token_name)
            if token:
                if _class.__name__ == "TwitchStream":
                    raw_stream["token"] = token.get("client_id")
                    raw_stream["bearer"] = self.ttv_bearer_cache.get("access_token", None)
                else:
                    raw_stream["token"] = token
            streams.append(_class(**raw_stream))

        return streams

    async def save_streams(self):
        raw_streams = []
        for stream in self.streams:
            raw_streams.append(stream.export())

        await self.config.streams.set(raw_streams)

    def cog_unload(self):
        if self.task:
            self.task.cancel()

    __del__ = cog_unload