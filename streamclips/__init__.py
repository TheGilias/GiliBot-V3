from .streamclips import StreamClips

async def setup(bot):
    cog = StreamClips(bot)
    await bot.add_cog(cog)