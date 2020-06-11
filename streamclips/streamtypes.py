import json
import logging
from random import choice
from string import ascii_letters
import xml.etree.ElementTree as ET
from typing import ClassVar, Optional, List

import aiohttp
import discord

from .errors import (
    APIError,
    OfflineStream,
    InvalidTwitchCredentials,
    InvalidYoutubeCredentials,
    StreamNotFound,
)
from datetime import datetime
from redbot.core.utils.chat_formatting import humanize_number

TWITCH_BASE_URL = "https://api.twitch.tv"
TWITCH_ID_ENDPOINT = TWITCH_BASE_URL + "/helix/users"
TWITCH_STREAMS_ENDPOINT = TWITCH_BASE_URL + "/helix/streams/"
TWITCH_COMMUNITIES_ENDPOINT = TWITCH_BASE_URL + "/helix/communities"

YOUTUBE_BASE_URL = "https://www.googleapis.com/youtube/v3"
YOUTUBE_CHANNELS_ENDPOINT = YOUTUBE_BASE_URL + "/channels"
YOUTUBE_SEARCH_ENDPOINT = YOUTUBE_BASE_URL + "/search"
YOUTUBE_VIDEOS_ENDPOINT = YOUTUBE_BASE_URL + "/videos"
YOUTUBE_CHANNEL_RSS = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

log = logging.getLogger("redbot.GiliBot-V3.StreamClips")


def rnd(url):
    """Appends a random parameter to the url to avoid Discord's caching"""
    return url + "?rnd=" + "".join([choice(ascii_letters) for _loop_counter in range(6)])


def get_video_ids_from_feed(feed):
    root = ET.fromstring(feed)
    rss_video_ids = []
    for child in root.iter("{http://www.w3.org/2005/Atom}entry"):
        for i in child.iter("{http://www.youtube.com/xml/schemas/2015}videoId"):
            yield i.text


class Stream:

    token_name: ClassVar[Optional[str]] = None

    def __init__(self, **kwargs):
        self.name = kwargs.pop("name", None)
        self.channels = kwargs.pop("channels", [])
        # self.already_online = kwargs.pop("already_online", False)
        self.last_checked = kwargs.pop("lastchecked", datetime.utcnow().isoformat())
        self.type = self.__class__.__name__

    async def get_clips(self):
        raise NotImplementedError()

    async def is_online(self):
        raise NotImplementedError()

    def make_embed(self):
        raise NotImplementedError()

    def make_clip_embeds(self):
        raise NotImplementedError()

    def export(self):
        data = {}
        for k, v in self.__dict__.items():
            if not k.startswith("_"):
                data[k] = v
        return data

    def __repr__(self):
        return "<{0.__class__.__name__}: {0.name}>".format(self)


class YoutubeStream(Stream):

    token_name = "youtube"

    def __init__(self, **kwargs):
        self.id = kwargs.pop("id", None)
        self._token = kwargs.pop("token", None)
        self.not_livestreams: List[str] = []
        self.livestreams: List[str] = []

        super().__init__(**kwargs)

    async def is_online(self):
        if not self._token:
            raise InvalidYoutubeCredentials("YouTube API key is not set.")

        if not self.id:
            self.id = await self.fetch_id()
        elif not self.name:
            self.name = await self.fetch_name()

        async with aiohttp.ClientSession() as session:
            async with session.get(YOUTUBE_CHANNEL_RSS.format(channel_id=self.id)) as r:
                rssdata = await r.text()

        if self.not_livestreams:
            self.not_livestreams = list(dict.fromkeys(self.not_livestreams))

        if self.livestreams:
            self.livestreams = list(dict.fromkeys(self.livestreams))

        for video_id in get_video_ids_from_feed(rssdata):
            if video_id in self.not_livestreams:
                log.debug(f"video_id in not_livestreams: {video_id}")
                continue
            log.debug(f"video_id not in not_livestreams: {video_id}")
            params = {
                "key": self._token["api_key"],
                "id": video_id,
                "part": "id,liveStreamingDetails",
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(YOUTUBE_VIDEOS_ENDPOINT, params=params) as r:
                    data = await r.json()
                    stream_data = data.get("items", [{}])[0].get("liveStreamingDetails", {})
                    log.debug(f"stream_data for {video_id}: {stream_data}")
                    if (
                        stream_data
                        and stream_data != "None"
                        and stream_data.get("actualStartTime", None) is not None
                        and stream_data.get("actualEndTime", None) is None
                    ):
                        if video_id not in self.livestreams:
                            self.livestreams.append(data["items"][0]["id"])
                    else:
                        self.not_livestreams.append(data["items"][0]["id"])
                        if video_id in self.livestreams:
                            self.livestreams.remove(video_id)
        log.debug(f"livestreams for {self.name}: {self.livestreams}")
        log.debug(f"not_livestreams for {self.name}: {self.not_livestreams}")
        # This is technically redundant since we have the
        # info from the RSS ... but incase you dont wanna deal with fully rewritting the
        # code for this part, as this is only a 2 quota query.
        if self.livestreams:
            params = {"key": self._token["api_key"], "id": self.livestreams[-1], "part": "snippet"}
            async with aiohttp.ClientSession() as session:
                async with session.get(YOUTUBE_VIDEOS_ENDPOINT, params=params) as r:
                    data = await r.json()
            return self.make_embed(data)
        raise OfflineStream()

    def make_embed(self, data):
        vid_data = data["items"][0]
        video_url = "https://youtube.com/watch?v={}".format(vid_data["id"])
        title = vid_data["snippet"]["title"]
        thumbnail = vid_data["snippet"]["thumbnails"]["medium"]["url"]
        channel_title = vid_data["snippet"]["channelTitle"]
        embed = discord.Embed(title=title, url=video_url)
        embed.set_author(name=channel_title)
        embed.set_image(url=rnd(thumbnail))
        embed.colour = 0x9255A5
        return embed

    async def fetch_id(self):
        return await self._fetch_channel_resource("id")

    async def fetch_name(self):
        snippet = await self._fetch_channel_resource("snippet")
        return snippet["title"]

    async def _fetch_channel_resource(self, resource: str):

        params = {"key": self._token["api_key"], "part": resource}
        if resource == "id":
            params["forUsername"] = self.name
        else:
            params["id"] = self.id

        async with aiohttp.ClientSession() as session:
            async with session.get(YOUTUBE_CHANNELS_ENDPOINT, params=params) as r:
                data = await r.json()

        if (
            "error" in data
            and data["error"]["code"] == 400
            and data["error"]["errors"][0]["reason"] == "keyInvalid"
        ):
            raise InvalidYoutubeCredentials()
        elif "items" in data and len(data["items"]) == 0:
            raise StreamNotFound()
        elif "items" in data:
            return data["items"][0][resource]
        raise APIError()

    def __repr__(self):
        return "<{0.__class__.__name__}: {0.name} (ID: {0.id})>".format(self)


class TwitchStream(Stream):

    token_name = "twitch"

    def __init__(self, **kwargs):
        self.id = kwargs.pop("id", None)
        self._client_id = kwargs.pop("token", None)
        self._bearer = kwargs.pop("bearer", None)
        super().__init__(**kwargs)

    async def is_online(self):
        if not self.id:
            self.id = await self.fetch_id()

        url = TWITCH_STREAMS_ENDPOINT
        header = {"Client-ID": str(self._client_id)}
        if self._bearer is not None:
            header = {**header, "Authorization": f"Bearer {self._bearer}"}
        params = {"user_id": self.id}

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=header, params=params) as r:
                data = await r.json(encoding="utf-8")
        if r.status == 200:
            if not data["data"]:
                raise OfflineStream()
            self.name = data["data"][0]["user_name"]
            data = data["data"][0]
            data["game_name"] = None
            data["followers"] = None
            data["view_count"] = None
            data["profile_image_url"] = None
            data["login"] = None

            game_id = data["game_id"]
            if game_id:
                params = {"id": game_id}
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        "https://api.twitch.tv/helix/games", headers=header, params=params
                    ) as r:
                        game_data = await r.json(encoding="utf-8")
                if game_data:
                    game_data = game_data["data"][0]
                    data["game_name"] = game_data["name"]
            params = {"to_id": self.id}
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://api.twitch.tv/helix/users/follows", headers=header, params=params
                ) as r:
                    user_data = await r.json(encoding="utf-8")
            if user_data:
                followers = user_data["total"]
                data["followers"] = followers

            params = {"id": self.id}
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://api.twitch.tv/helix/users", headers=header, params=params
                ) as r:
                    user_profile_data = await r.json(encoding="utf-8")
            if user_profile_data:
                profile_image_url = user_profile_data["data"][0]["profile_image_url"]
                data["profile_image_url"] = profile_image_url
                data["view_count"] = user_profile_data["data"][0]["view_count"]
                data["login"] = user_profile_data["data"][0]["login"]

            is_rerun = False
            return self.make_embed(data), is_rerun
        elif r.status == 400:
            raise InvalidTwitchCredentials()
        elif r.status == 404:
            raise StreamNotFound()
        else:
            raise APIError()

    async def fetch_id(self):
        header = {"Client-ID": str(self._client_id)}
        if self._bearer is not None:
            header = {**header, "Authorization": f"Bearer {self._bearer}"}
        url = TWITCH_ID_ENDPOINT
        params = {"login": self.name}

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=header, params=params) as r:
                data = await r.json()

        if r.status == 200:
            if not data["data"]:
                raise StreamNotFound()
            return data["data"][0]["id"]
        elif r.status == 400:
            raise StreamNotFound()
        elif r.status == 401:
            raise InvalidTwitchCredentials()
        else:
            raise APIError()

    def make_embed(self, data):
        is_rerun = data["type"] == "rerun"
        url = f"https://www.twitch.tv/{data['login']}" if data["login"] is not None else None
        logo = data["profile_image_url"]
        if logo is None:
            logo = "https://static-cdn.jtvnw.net/jtv_user_pictures/xarth/404_user_70x70.png"
        status = data["title"]
        if not status:
            status = ("Untitled broadcast")
        if is_rerun:
            status += (" - Rerun")
        embed = discord.Embed(title=status, url=url, color=0x6441A4)
        embed.set_author(name=data["user_name"])
        embed.add_field(name=("Followers"), value=humanize_number(data["followers"]))
        embed.add_field(name=("Total views"), value=humanize_number(data["view_count"]))
        embed.set_thumbnail(url=logo)
        if data["thumbnail_url"]:
            embed.set_image(url=rnd(data["thumbnail_url"].format(width=320, height=180)))
        if data["game_name"]:
            embed.set_footer(text=("Playing: ") + data["game_name"])
        return embed

    def __repr__(self):
        return "<{0.__class__.__name__}: {0.name} (ID: {0.id})>".format(self)


class HitboxStream(Stream):

    token_name = None  # This streaming services don't currently require an API key

    async def is_online(self):
        url = "https://api.hitbox.tv/media/live/" + self.name

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as r:
                # data = await r.json(encoding='utf-8')
                data = await r.text()
        data = json.loads(data, strict=False)
        if "livestream" not in data:
            raise StreamNotFound()
        elif data["livestream"][0]["media_is_live"] == "0":
            # self.already_online = False
            raise OfflineStream()
        elif data["livestream"][0]["media_is_live"] == "1":
            # self.already_online = True
            return self.make_embed(data)

        raise APIError()

    def make_embed(self, data):
        base_url = "https://edge.sf.hitbox.tv"
        livestream = data["livestream"][0]
        channel = livestream["channel"]
        url = channel["channel_link"]
        embed = discord.Embed(title=livestream["media_status"], url=url, color=0x98CB00)
        embed.set_author(name=livestream["media_name"])
        embed.add_field(name=("Followers"), value=humanize_number(channel["followers"]))
        embed.set_thumbnail(url=base_url + channel["user_logo"])
        if livestream["media_thumbnail"]:
            embed.set_image(url=rnd(base_url + livestream["media_thumbnail"]))
        embed.set_footer(text=("Playing: ") + livestream["category_name"])

        return embed


class MixerStream(Stream):

    token_name = None  # This streaming services don't currently require an API key

    async def get_clips(self):
        url = "https://mixer.com/api/v1/clips/channels/" + self.name
        clip_embeds = []

        log.debug("Obtaining clip list from URL" + url)
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as r:
                data = await r.text(encoding="utf-8")
        if r.status == 200:
            #data = json.loads(data, strict=False)
            #log.info (f"{len(data)} clips found")
            #for currentitem in data.items():
            #    clip_embeds += self.make_clip_embeds(currentitem)
            
            #return clip_embeds
        elif r.status == 404:
            raise StreamNotFound()
        else:
            raise APIError()

    async def is_online(self):
        url = "https://mixer.com/api/v1/channels/" + self.name

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as r:
                data = await r.text(encoding="utf-8")
        if r.status == 200:
            data = json.loads(data, strict=False)
            if data["online"] is True:
                # self.already_online = True
                return self.make_embed(data)
            else:
                # self.already_online = False
                raise OfflineStream()
        elif r.status == 404:
            raise StreamNotFound()
        else:
            raise APIError()

    def make_embed(self, data):
        default_avatar = "https://mixer.com/_latest/assets/images/main/avatars/default.jpg"
        user = data["user"]
        url = "https://mixer.com/" + data["token"]
        embed = discord.Embed(title=data["name"], url=url)
        embed.set_author(name=user["username"])
        embed.add_field(name=("Followers"), value=humanize_number(data["numFollowers"]))
        embed.add_field(name=("Total views"), value=humanize_number(data["viewersTotal"]))
        if user["avatarUrl"]:
            embed.set_thumbnail(url=user["avatarUrl"])
        else:
            embed.set_thumbnail(url=default_avatar)
        if data["thumbnail"]:
            embed.set_image(url=rnd(data["thumbnail"]["url"]))
        embed.color = 0x4C90F3  # pylint: disable=assigning-non-slot
        if data["type"] is not None:
            embed.set_footer(text=("Playing: ") + data["type"]["name"])
        return embed

    def make_clip_embeds(self, data):
        default_avatar = "https://mixer.com/_latest/assets/images/main/avatars/default.jpg"
        user = data["user"]
        url = "https://mixer.com/" + data["token"]
        embed = discord.Embed(title=data["title"], url=url)
        embed.set_author(name=user["username"])
        if user["avatarUrl"]:
            embed.set_thumbnail(url=user["avatarUrl"])
        else:
            embed.set_thumbnail(url=default_avatar)
        if data["thumbnail"]:
            embed.set_image(url=rnd(data["thumbnail"]["uri"]))
        embed.color = 0x4C90F3  # pylint: disable=assigning-non-slot
        
class PicartoStream(Stream):

    token_name = None  # This streaming services don't currently require an API key

    async def is_online(self):
        url = "https://api.picarto.tv/v1/channel/name/" + self.name

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as r:
                data = await r.text(encoding="utf-8")
        if r.status == 200:
            data = json.loads(data)
            if data["online"] is True:
                # self.already_online = True
                return self.make_embed(data)
            else:
                # self.already_online = False
                raise OfflineStream()
        elif r.status == 404:
            raise StreamNotFound()
        else:
            raise APIError()

    def make_embed(self, data):
        avatar = rnd(
            "https://picarto.tv/user_data/usrimg/{}/dsdefault.jpg".format(data["name"].lower())
        )
        url = "https://picarto.tv/" + data["name"]
        thumbnail = data["thumbnails"]["web"]
        embed = discord.Embed(title=data["title"], url=url, color=0x4C90F3)
        embed.set_author(name=data["name"])
        embed.set_image(url=rnd(thumbnail))
        embed.add_field(name=("Followers"), value=humanize_number(data["followers"]))
        embed.add_field(name=("Total views"), value=humanize_number(data["viewers_total"]))
        embed.set_thumbnail(url=avatar)
        data["tags"] = ", ".join(data["tags"])

        if not data["tags"]:
            data["tags"] = ("None")

        if data["adult"]:
            data["adult"] = ("NSFW | ")
        else:
            data["adult"] = ""

        embed.set_footer(text=("{adult}Category: {category} | Tags: {tags}").format(**data))
        return embed
