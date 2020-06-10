import discord
from redbot.core.bot import Red
from redbot.core import checks, commands, Config
from redbot.core.i18n import cog_i18n, Translator
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

_ = Translator("StreamClips", __file__)
log = logging.getLogger("red.core.cogs.StreamClips")

class StreamClips(commands.Cog):
    """This cog is designed to put alerts in a text channel when new clips from specified streamers are detected."""

    @commands.command()
    async def streamclips(self, ctx):
        """This does stuff!"""
        # Your code will go here
        await ctx.send("I can do stuff too!")