import asyncio
import logging
from datetime import datetime

import discord
import openai
from aiocache import cached
from redbot.core.utils.chat_formatting import humanize_list

from .abc import MixinMeta
from .models import GuildSettings

log = logging.getLogger("red.vrt.assistant.api")


class API(MixinMeta):
    @cached(ttl=120)
    async def get_chat_response(
        self, message: str, author: discord.Member, conf: GuildSettings
    ) -> str:
        conversation = self.chats.get_conversation(author)
        timestamp = f"<t:{round(datetime.now().timestamp())}:F>"
        created = f"<t:{round(author.guild.created_at.timestamp())}:F>"
        date = datetime.now().astimezone().strftime("%B %d, %Y")
        time = datetime.now().astimezone().strftime("%I:%M %p %Z")
        roles = [role.name for role in author.roles]
        params = {
            "botname": self.bot.user.name,
            "timestamp": timestamp,
            "date": date,
            "time": time,
            "members": author.guild.member_count,
            "user": author.display_name,
            "datetime": str(datetime.now()),
            "roles": humanize_list(roles),
            "avatar": author.avatar.url if author.avatar else "",
            "owner": author.guild.owner,
            "servercreated": created,
            "server": author.guild.name,
            "messages": len(conversation.messages),
            "characters": conversation.character_count(conf, message),
            "retention": conf.max_retention,
            "retentiontime": conf.max_retention_time,
        }

        system_prompt = conf.system_prompt.format(**params)
        initial_prompt = conf.prompt.format(**params)

        conversation.update_messages(conf, message, "user")
        messages = conversation.prepare_chat(system_prompt, initial_prompt)

        response = await asyncio.to_thread(self.call_openai, conf, messages)
        try:
            reply = response["choices"][0]["message"]["content"]
            # usage = response["usage"]
            # total = usage['total_tokens']
        except KeyError:
            reply = str(response)
            # usage = None
        conversation.update_messages(conf, reply, "assistant")
        return reply

    def call_openai(self, conf: GuildSettings, messages: dict) -> dict:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=messages,
            temperature=0,
            api_key=conf.api_key,
        )
        return response
