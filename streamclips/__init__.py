from .streamclips import StreamClips

def setup(bot):
    cog = StreamClips(bot)
    bot.add_cog(cog)