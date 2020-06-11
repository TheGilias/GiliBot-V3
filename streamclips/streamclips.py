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
            self.task = self.bot.loop.create_task(self._stream_alerts())
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

    def cog_unload(self):
        if self.task:
            self.task.cancel()

    __del__ = cog_unload