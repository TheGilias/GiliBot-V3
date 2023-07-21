from redbot.core.bot import Red

from .streamclips import StreamClips

async def setup(bot: Red) -> None:
    cog = StreamClips(bot)
    await bot.add_cog(cog)