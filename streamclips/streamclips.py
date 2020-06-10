from redbot.core import commands

class StreamClips(commands.Cog):
    """My custom cog"""

    @commands.command()
    async def streamclips(self, ctx):
        """This does stuff!"""
        # Your code will go here
        await ctx.send("I can do stuff!")