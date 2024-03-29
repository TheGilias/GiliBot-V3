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
    OfflineStream,
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

log = logging.getLogger("red.GiliBot-V3.StreamClips")

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
                message = (
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

    @clipalert.command(name="twitch")
    async def twitch_clip_alert(self, ctx: commands.Context, channel_name: str):
        """Toggle alerts in this channel for a Twitch channel's clips."""
        await self.stream_clip_alert(ctx, TwitchStream, channel_name)

    @clipalert.command(name="mixer")
    async def mixer_clip_alert(self, ctx: commands.Context, channel_name: str):
        """Toggle alerts in this channel for a Mixer channel's clips."""
        await self.stream_clip_alert(ctx, MixerStream, channel_name)

    @clipalert.command(name="stop", usage="[disable_all=No]")
    async def clipalert_stop(self, ctx: commands.Context, _all: bool = False):
        """Disable all stream clip alerts in this channel or server.

        `[p]clipalert stop` will disable this channel's stream
        clip alerts.

        Do `[p]clipalert stop yes` to disable all stream clip alerts 
        in this server.
        """
        streams = self.streams.copy()
        local_channel_ids = [c.id for c in ctx.guild.channels]
        to_remove = []

        for stream in streams:
            for channel_id in stream.channels:
                if channel_id == ctx.channel.id:
                    stream.channels.remove(channel_id)
                elif _all and ctx.channel.id in local_channel_ids:
                    if channel_id in stream.channels:
                        stream.channels.remove(channel_id)

            if not stream.channels:
                to_remove.append(stream)

        for stream in to_remove:
            streams.remove(stream)

        self.streams = streams
        await self.save_streams()

        if _all:
            msg = ("All the clip alerts in this server have been disabled.")
        else:
            msg = ("All the clip alerts in this channel have been disabled.")

        await ctx.send(msg)

    @clipalert.command(name="list")
    async def clipalert_list(self, ctx: commands.Context):
        """List all active clip alerts in this server."""
        streams_list = defaultdict(list)
        guild_channels_ids = [c.id for c in ctx.guild.channels]
        msg = ("Active alerts:\n\n")

        for stream in self.streams:
            for channel_id in stream.channels:
                if channel_id in guild_channels_ids:
                    streams_list[channel_id].append(stream.name.lower())

        if not streams_list:
            await ctx.send(("There are no active alerts in this server."))
            return

        for channel_id, streams in streams_list.items():
            channel = ctx.guild.get_channel(channel_id)
            msg += "** - #{}**\n{}\n".format(channel, ", ".join(streams))

        for page in pagify(msg):
            await ctx.send(page)

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
                    bearer=self.ttv_bearer_cache.get("access_token", None)
                )
            else:
                stream = _class(name=channel_name, token=token)
            try:
                exists = await self.check_exists(stream)
            except InvalidTwitchCredentials:
                await ctx.send(
                    (
                        "The Twitch token is either invalid or has not been set. See "
                        "`{prefix}clipset twitchtoken`."
                    ).format(prefix=ctx.clean_prefix)
                )
                return
            except InvalidYoutubeCredentials:
                await ctx.send(
                    (
                        "The YouTube API key is either invalid or has not been set. See "
                        "`{prefix}clipset youtubekey`."
                    ).format(prefix=ctx.clean_prefix)
                )
                return
            except APIError:
                await ctx.send(
                    ("Something went wrong whilst trying to contact the stream service's API.")
                )
                return
            else:
                if not exists:
                    await ctx.send(("That channel doesn't seem to exist."))
                    return
            await stream.seed_new_streamer(log)
            
        await self.add_or_remove(ctx, stream)

    @commands.group()
    @checks.mod()
    async def clipset(self, ctx: commands.Context):
        """Set tokens and refresh settings."""
        pass

    @clipset.command(name="timer")
    @checks.is_owner()
    async def _clipset_refresh_timer(self, ctx: commands.Context, refresh_time: int):
        """Set clip check refresh time."""
        if refresh_time < 60:
            return await ctx.send(_("You cannot set the refresh timer to less than 60 seconds"))

        await self.config.refresh_timer.set(refresh_time)
        await ctx.send(
            ("Refresh timer set to {refresh_time} seconds".format(refresh_time=refresh_time))
        )

    @clipset.command()
    @checks.is_owner()
    async def twitchtoken(self, ctx: commands.Context):
        """Explain how to set the twitch token."""
        message = (
            "To set the twitch API tokens, follow these steps:\n"
            "1. Go to this page: https://dev.twitch.tv/dashboard/apps.\n"
            "2. Click *Register Your Application*.\n"
            "3. Enter a name, set the OAuth Redirect URI to `http://localhost`, and "
            "select an Application Category of your choosing.\n"
            "4. Click *Register*.\n"
            "5. Copy your client ID and your client secret into:\n"
            "{command}"
            "\n\n"
            "Note: These tokens are sensitive and should only be used in a private channel\n"
            "or in DM with the bot.\n"
        ).format(
            command="`{}set api twitch client_id {} client_secret {}`".format(
                ctx.clean_prefix, ("<your_client_id_here>"), ("<your_client_secret_here>")
            )
        )

        await ctx.maybe_send_embed(message)

    async def add_or_remove(self, ctx: commands.Context, stream):
        if ctx.channel.id not in stream.channels:
            stream.channels.append(ctx.channel.id)
            if stream not in self.streams:
                self.streams.append(stream)
            await ctx.send(
                (
                    "I'll now send a notification in this channel when {stream.name} has new clips."
                ).format(stream=stream)
            )
        else:
            stream.channels.remove(ctx.channel.id)
            if not stream.channels:
                self.streams.remove(stream)
            await ctx.send(
                (
                    "I won't send notifications about {stream.name} clips in this channel anymore."
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
        log.debug("Checking streamers for clips")

        for stream in self.streams:
            log.debug(f"Checking for new {stream.__class__.__name__ } clips from {stream.name}")

            with contextlib.suppress(Exception):
                #try:
                if stream.__class__.__name__ == "TwitchStream":
                    await self.maybe_renew_twitch_bearer_token()
                    embeds = await stream.get_new_clips(log)
                else:
                    embeds = await stream.get_new_clips(log)
                log.debug (f"{len(embeds)} clips found in get_new_clips")
                await self.save_streams()
                
                for channel_id in stream.channels:
                    channel = self.bot.get_channel(channel_id)
                    if not channel:
                        continue
                    mention_str, edited_roles = await self._get_mention_str(channel.guild)
                    
                    if mention_str:
                        alert_msg = await self.config.guild(
                            channel.guild
                        ).live_message_mention()
                        if alert_msg:
                            content = alert_msg.format(mention=mention_str, stream=stream)
                        else:
                            content = ("{mention}, {stream} has a new clip!").format(
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
                            content = ("{stream} has a new clip!").format(
                                stream=escape(
                                    str(stream.name), mass_mentions=True, formatting=True
                                )
                            )
                    for embed in embeds:
                        m = await channel.send(content, embed=embed)
                        if edited_roles:
                            for role in edited_roles:
                                await role.edit(mentionable=False)
                    #stream.last_checked = datetime.utcnow().isoformat() # Update the last checked time now that we're at the end.
                    await self.save_streams()
                #except Exception as e:
                #    log.error (f"Failed clip search/post/save with error {e}, {e.__traceback__}")

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