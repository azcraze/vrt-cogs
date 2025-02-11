import asyncio
import json
import logging

import discord
from redbot.core.i18n import Translator, cog_i18n

from ..abc import MixinMeta
from .models import EmbeddingEntryExists

log = logging.getLogger("red.vrt.assistant.functions")
_ = Translator("Assistant", __file__)


@cog_i18n(_)
class AssistantFunctions(MixinMeta):
    async def create_memory(
        self,
        guild: discord.Guild,
        user: discord.Member,
        memory_name: str,
        memory_text: str,
        *args,
        **kwargs,
    ):
        if len(memory_name) > 100:
            return "Error: memory_name should be 100 characters or less!"
        conf = self.db.get_conf(guild)
        if not any([role.id in conf.tutors for role in user.roles]) and user.id not in conf.tutors:
            return f"User {user.display_name} is not recognized as a tutor!"
        try:
            embedding = await self.add_embedding(
                guild,
                memory_name,
                memory_text,
                overwrite=False,
                ai_created=True,
            )
            if embedding is None:
                return "Failed to create memory"
            return f"The memory '{memory_name}' has been created successfully"
        except EmbeddingEntryExists:
            return "That memory name already exists"

    async def search_memories(
        self,
        guild: discord.Guild,
        search_query: str,
        amount: int = 2,
        *args,
        **kwargs,
    ):
        try:
            amount = int(amount)
        except ValueError:
            return "Error: amount must be an integer"
        if amount < 1:
            return "Amount needs to be more than 1"

        conf = self.db.get_conf(guild)
        if not conf.embeddings:
            return "There are no memories saved!"

        if search_query in conf.embeddings:
            embed = conf.embeddings[search_query]
            return f"Found a memory name that matches exactly: {embed.text}"

        query_embedding = await self.request_embedding(search_query, conf)
        if not query_embedding:
            return f"Failed to get memory for your the query '{search_query}'"

        embeddings = await asyncio.to_thread(
            conf.get_related_embeddings,
            query_embedding=query_embedding,
            top_n_override=amount,
            relatedness_override=0.5,
        )
        if not embeddings:
            return f"No embeddings could be found related to the search query '{search_query}'"

        results = []
        for embed in embeddings:
            entry = {"memory name": embed[0], "relatedness": embed[2], "content": embed[1]}
            results.append(entry)

        return f"Memories related to `{search_query}`\n{json.dumps(results, indent=2)}"

    async def edit_memory(
        self,
        guild: discord.Guild,
        user: discord.Member,
        memory_name: str,
        memory_text: str,
        *args,
        **kwargs,
    ):
        conf = self.db.get_conf(guild)
        if not any([role.id in conf.tutors for role in user.roles]) and user.id not in conf.tutors:
            return f"User {user.display_name} is not recognized as a tutor!"

        if memory_name not in conf.embeddings:
            return "A memory with that name does not exist!"
        embedding = await self.request_embedding(memory_text, conf)
        if not embedding:
            return "Could not update the memory!"

        conf.embeddings[memory_name].text = memory_text
        conf.embeddings[memory_name].embedding = embedding
        conf.embeddings[memory_name].update()
        asyncio.create_task(self.save_conf())
        return "Your memory has been updated!"

    async def list_memories(
        self,
        guild: discord.Guild,
        *args,
        **kwargs,
    ):
        conf = self.db.get_conf(guild)
        if not conf.embeddings:
            return "You have no memories available!"
        joined = "\n".join([i for i in conf.embeddings])
        return joined
