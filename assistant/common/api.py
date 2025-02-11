import asyncio
import inspect
import json
import logging
import math
from typing import List, Optional, Tuple

import aiohttp
import discord
import tiktoken
from aiohttp import ClientConnectionError
from openai.types.chat.chat_completion import ChatCompletion
from openai.types.chat.chat_completion_message import ChatCompletionMessage
from openai.types.completion import Completion
from openai.types.completion_choice import CompletionChoice
from openai.types.create_embedding_response import CreateEmbeddingResponse
from perftracker import perf
from redbot.core import commands
from redbot.core.i18n import Translator, cog_i18n
from redbot.core.utils.chat_formatting import box, humanize_number

from ..abc import MixinMeta
from .calls import (
    request_chat_completion_raw,
    request_completion_raw,
    request_embedding_raw,
    request_text_raw,
    request_tokens_raw,
)
from .constants import CHAT, MODELS
from .models import GuildSettings
from .utils import compile_messages

log = logging.getLogger("red.vrt.assistant.api")
_ = Translator("Assistant", __file__)


@cog_i18n(_)
class API(MixinMeta):
    @perf()
    async def openai_status(self) -> str:
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url="https://status.openai.com/api/v2/status.json") as res:
                    data = await res.json()
                    status = data["status"]["description"]
                    # ind = data["status"]["indicator"]
        except Exception as e:
            log.error("Failed to fetch OpenAI API status", exc_info=e)
            status = _("Failed to fetch!")
        return status

    async def request_response(
        self,
        messages: List[dict],
        conf: GuildSettings,
        functions: Optional[List[dict]] = None,
        member: Optional[discord.Member] = None,
        response_token_override: int = None,
    ) -> ChatCompletionMessage:
        api_base = conf.endpoint_override or self.db.endpoint_override
        api_key = "unset"
        if conf.api_key:
            api_base = None
            api_key = conf.api_key

        model = conf.get_user_model(member)

        max_convo_tokens = self.get_max_tokens(conf, member)
        max_response_tokens = conf.get_user_max_response_tokens(member)

        # Overestimate by 5%
        current_convo_tokens = await self.count_payload_tokens(messages, conf, model)
        if functions:
            current_convo_tokens += await self.count_function_tokens(functions, conf, model)

        current_convo_tokens = round(current_convo_tokens * 1.05)

        # Dynamically adjust to lower model to save on cost
        if "-16k" in model and current_convo_tokens < 2000:
            model = model.replace("-16k", "")
        if "-32k" in model and current_convo_tokens < 4000:
            model = model.replace("-32k", "")

        max_model_tokens = MODELS[model]

        # Ensure that user doesn't set max response tokens higher than model can handle
        if response_token_override:
            response_tokens = response_token_override
        else:
            response_tokens = 0  # Dynamic
            if max_response_tokens:
                # Calculate max response tokens
                response_tokens = max(max_convo_tokens - current_convo_tokens, 0)
                # If current convo exceeds the max convo tokens for that user, use max model tokens
                if not response_tokens:
                    response_tokens = max(max_model_tokens - current_convo_tokens, 0)
                # Use the lesser of caculated vs set response tokens
                response_tokens = min(response_tokens, max_response_tokens)

        if model in CHAT:
            response: ChatCompletion = await request_chat_completion_raw(
                model=model,
                messages=messages,
                temperature=conf.temperature,
                api_key=api_key,
                max_tokens=response_tokens,
                api_base=api_base,
                functions=functions,
                frequency_penalty=conf.frequency_penalty,
                presence_penalty=conf.presence_penalty,
                seed=conf.seed,
            )
            message: ChatCompletionMessage = response.choices[0].message
        else:
            compiled = compile_messages(messages)
            prompt = await self.cut_text_by_tokens(compiled, conf, member)
            response: Completion = await request_completion_raw(
                model=model,
                prompt=prompt,
                temperature=conf.temperature,
                api_key=api_key,
                max_tokens=response_tokens,
                api_base=api_base,
            )
            choice: CompletionChoice = response.choices[0]
            message = ChatCompletionMessage.model_validate({"role": "assistant", "content": choice.text})

        conf.update_usage(
            response.model,
            response.usage.total_tokens,
            response.usage.prompt_tokens,
            response.usage.completion_tokens,
        )
        log.debug(f"MESSAGE TYPE: {type(message)}")
        return message

    async def request_embedding(self, text: str, conf: GuildSettings) -> List[float]:
        if conf.api_key:
            api_base = None
            api_key = conf.api_key
        else:
            log.debug("Using external embedder")
            api_base = conf.endpoint_override or self.db.endpoint_override
            api_key = "unset"

        response: CreateEmbeddingResponse = await request_embedding_raw(text, api_key, api_base)

        conf.update_usage(
            response.model,
            response.usage.total_tokens,
            response.usage.prompt_tokens,
            0,
        )
        return response.data[0].embedding

    # -------------------------------------------------------
    # -------------------------------------------------------
    # ----------------------- HELPERS -----------------------
    # -------------------------------------------------------
    # -------------------------------------------------------

    async def count_payload_tokens(
        self,
        messages: List[dict],
        conf: GuildSettings,
        model: str = "gpt-3.5-turbo-0613",
    ):
        if not messages:
            return 0
        if not conf.api_key and (conf.endpoint_override or self.db.endpoint_override):
            log.debug("Using external tokenizer")
            endpoint = conf.endpoint_override or self.db.endpoint_override
            num_tokens = 0
            valid_endpoint = True
            for message in messages:
                if not valid_endpoint:
                    break
                num_tokens += 4  # every message follows <im_start>{role/name}\n{content}<im_end>\n
                for key, value in message.items():
                    if not value:
                        continue
                    if isinstance(value, list):
                        for i in value:
                            if i["type"] == "image_url":
                                num_tokens += 65
                                continue
                            try:
                                tokens = await request_tokens_raw(i, f"{endpoint}/tokenize")
                                num_tokens += len(tokens)
                            except (KeyError, ClientConnectionError):  # API probably old or bad endpoint
                                # Break and fall back to local encoder
                                valid_endpoint = False
                    try:
                        tokens = await request_tokens_raw(value, f"{endpoint}/tokenize")
                        num_tokens += len(tokens)
                        if key == "name":  # if there's a name, the role is omitted
                            num_tokens += -1  # role is always required and always 1 token
                    except (KeyError, ClientConnectionError):  # API probably old or bad endpoint
                        # Break and fall back to local encoder
                        valid_endpoint = False
                num_tokens += 2  # every reply is primed with <im_start>assistant
            else:
                return num_tokens
        try:
            encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            encoding = tiktoken.get_encoding("cl100k_base")

        num_tokens = 0
        for message in messages:
            num_tokens += 4  # every message follows <im_start>{role/name}\n{content}<im_end>\n
            for key, value in message.items():
                if not value:
                    continue
                if isinstance(value, list):
                    for i in value:
                        if i["type"] == "image_url":
                            num_tokens += 65
                            if i.get("detail", "") == "high":
                                num_tokens += 65
                            continue
                        try:
                            encoded = await asyncio.to_thread(encoding.encode, i.get("text") or str(i))
                        except Exception as e:
                            log.error(f"Failed to encode: {i.get('text') or str(i)}", exc_info=e)
                            encoded = []
                        num_tokens += len(encoded)
                else:
                    try:
                        encoded = await asyncio.to_thread(encoding.encode, str(value))
                    except Exception as e:
                        log.error(f"Failed to encode: {value}", exc_info=e)
                        encoded = []
                    num_tokens += len(encoded)
                if key == "name":  # if there's a name, the role is omitted
                    num_tokens += -1  # role is always required and always 1 token
        num_tokens += 2  # every reply is primed with <im_start>assistant
        return num_tokens

    async def count_function_tokens(
        self,
        functions: List[dict],
        conf: GuildSettings,
        model: str = "gpt-3.5-turbo-0613",
    ):
        if not conf.api_key and (conf.endpoint_override or self.db.endpoint_override):
            log.debug("Using external tokenizer")
            endpoint = conf.endpoint_override or self.db.endpoint_override
            num_tokens = 0
            for func in functions:
                dump = json.dumps(func)
                try:
                    tokens = await request_tokens_raw(dump, f"{endpoint}/tokenize")
                    num_tokens += len(tokens)
                except (KeyError, ClientConnectionError):  # API probably old or bad endpoint
                    # Break and fall back to local encoder
                    break
            else:
                return num_tokens

        try:
            encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            encoding = tiktoken.get_encoding("cl100k_base")
        num_tokens = 0
        for func in functions:
            dump = json.dumps(func)
            encoded = await asyncio.to_thread(encoding.encode, dump)
            num_tokens += len(encoded)
        return num_tokens

    async def get_tokens(
        self,
        text: str,
        conf: GuildSettings,
        model: str = "gpt-3.5-turbo-0613",
    ) -> list:
        """Get token list from text"""
        if not text:
            log.debug("No text to get tokens from!")
            return []
        if isinstance(text, bytes):
            text = text.decode(encoding="utf-8")

        if not conf.api_key and (conf.endpoint_override or self.db.endpoint_override):
            log.debug("Using external tokenizer")
            endpoint = conf.endpoint_override or self.db.endpoint_override
            try:
                return await request_tokens_raw(text, f"{endpoint}/tokenize")
            except (KeyError, ClientConnectionError):  # API probably old or bad endpoint
                pass

        def get_encoding():
            try:
                enc = tiktoken.encoding_for_model(model)
            except KeyError:
                enc = tiktoken.get_encoding("cl100k_base")
            return enc

        encoding = await asyncio.to_thread(get_encoding)

        return await asyncio.to_thread(encoding.encode, text)

    async def count_tokens(self, text: str, conf: GuildSettings, model: str) -> int:
        if not text:
            log.debug("No text to get token count from!")
            # raise Exception("No text to get token count from!")
            return 0
        tokens = await self.get_tokens(text, conf, model)
        return len(tokens)

    async def can_call_llm(self, conf: GuildSettings, ctx: Optional[commands.Context] = None) -> bool:
        cant = [
            not conf.api_key,
            conf.endpoint_override is None,
            self.db.endpoint_override is None,
        ]
        if all(cant):
            if ctx:
                txt = _("There are no API keys set!\n")
                if ctx.author.id == ctx.guild.owner_id:
                    txt += _("- Set your OpenAI key with `{}`\n").format(f"{ctx.clean_prefix}assist openaikey")
                    txt += _("- Or set an endpoint override to your self-hosted LLM with `{}`\n").format(
                        f"{ctx.clean_prefix}assist endpoint"
                    )
                if ctx.author.id in self.bot.owner_ids:
                    txt += _("- Alternatively you can set a global endpoint with `{}`").format(
                        f"{ctx.clean_prefix}assist globalendpoint"
                    )
                await ctx.send(txt)
            return False
        return True

    async def resync_embeddings(self, conf: GuildSettings) -> int:
        """Update embeds to match current dimensions

        Takes a sample using current embed method, the updates the rest to match dimensions
        """
        if not conf.embeddings:
            return 0

        sample = list(conf.embeddings.values())[0]
        sample_embed = await self.request_embedding(sample.text, conf)

        synced = 0
        for name, em in conf.embeddings.items():
            if len(em.embedding) != len(sample_embed):
                em.embedding = await self.request_embedding(em.text, conf)
                synced += 1
                log.debug(f"Updating embedding {name}")

        if synced:
            await self.save_conf()
        return synced

    def get_max_tokens(self, conf: GuildSettings, user: Optional[discord.Member]) -> int:
        user_max = conf.get_user_max_tokens(user)
        return min(user_max, MODELS[conf.get_user_model(user)] - 96)

    async def cut_text_by_tokens(self, text: str, conf: GuildSettings, user: Optional[discord.Member]) -> str:
        if not text:
            log.debug("No text to cut by tokens!")
            return text
        tokens = await self.get_tokens(text, conf, conf.get_user_model(user))
        return await self.get_text(tokens[: self.get_max_tokens(conf, user)], conf)

    async def get_text(self, tokens: list, conf: GuildSettings) -> str:
        """Get text from token list"""

        if not conf.api_key and (conf.endpoint_override or self.db.endpoint_override):
            log.debug("Using external tokenizer")
            endpoint = conf.endpoint_override or self.db.endpoint_override
            return await request_text_raw(tokens, f"{endpoint}/untokenize")

        return await asyncio.to_thread(self.tokenizer.decode, tokens)

    # -------------------------------------------------------
    # -------------------------------------------------------
    # -------------------- FORMATTING -----------------------
    # -------------------------------------------------------
    # -------------------------------------------------------
    @perf()
    async def degrade_conversation(
        self,
        messages: List[dict],
        function_list: List[dict],
        conf: GuildSettings,
        user: Optional[discord.Member],
    ) -> bool:
        """
        Iteratively degrade a conversation payload in-place to fit within the max token limit, prioritizing more recent messages and critical context.

        Order of importance:
        - System messages
        - Function calls available to model
        - Most recent user message
        - Most recent assistant message
        - Most recent function/tool message

        System messages are always ignored.

        Args:
            messages (List[dict]): message entries sent to the api
            function_list (List[dict]): list of json function schemas for the model
            conf: (GuildSettings): current settings

        Returns:
            bool: whether the conversation was degraded
        """

        def _degrade_text(txt: str) -> str:
            words = txt.split()
            if len(words) > 1:
                return " ".join(words[:-1])
            else:
                return ""

        def _most_recent():
            most_recent_user = most_recent_assistant = most_recent_tool = None
            for idx, msg in enumerate(reversed(messages)):
                if msg["role"] in ["tool", "function"] and not most_recent_tool:
                    most_recent_tool = len(messages) - 1 - idx
                elif msg["role"] == "assistant" and not most_recent_assistant:
                    most_recent_assistant = len(messages) - 1 - idx
                elif msg["role"] == "user" and not most_recent_user:
                    most_recent_user = len(messages) - 1 - idx
                elif most_recent_user and most_recent_assistant and most_recent_tool:
                    break
            return most_recent_user, most_recent_assistant, most_recent_tool

        # Fetch the current model
        model = conf.get_user_model(user)
        # Fetch the max response tokens for the current user
        conf.get_user_max_response_tokens(user)
        # Fetch the max token limit for the current user
        max_tokens = self.get_max_tokens(conf, user)

        # Token count of current conversation
        convo_tokens = await self.count_payload_tokens(messages, conf, model)
        # Token count of function calls available to model
        function_tokens = await self.count_function_tokens(function_list, conf, model)
        total_tokens_used = convo_tokens + function_tokens

        # Check if the total token count is already under the max token limit
        if total_tokens_used <= max_tokens:
            return False

        log.info(f"Degrading messages for {user} (total: {total_tokens_used}/max: {max_tokens})")
        # First we will iterate through the messages and remove in the following sweep order:
        # 1. Remove oldest tool call or response
        # 2. Remove oldest assistant message
        # 3. Remove oldest user message
        # Then we will repeat the process until we are under the max token limit
        # We will NOT remove the most recent user message or assistant message
        # We will also not touch system messages
        # We will also not touch function calls available to model (yet)

        most_recent_user, most_recent_assistant, most_recent_tool = _most_recent()

        # Start degrading the conversation except for system messages and most recent messages
        messages_to_purge = set()
        token_reduction = 0
        for idx, msg in enumerate(messages):
            skip_conditions = [
                msg["role"] == "system",
                idx == most_recent_user,
                idx == most_recent_assistant,
                idx == most_recent_tool,
            ]
            if any(skip_conditions):
                continue

            # This message will get popped
            token_reduction += 4  # Default count
            if "name" in msg:
                token_reduction += 1

            if msg["role"] in ["tool", "function"]:
                messages_to_purge.add(idx)
                token_reduction += await self.count_tokens(msg["content"], conf, model)
            elif msg["role"] == "assistant":
                messages_to_purge.add(idx)
                content = msg["content"] or msg.get("tool_calls", "") or msg.get("function_call", "")
                token_reduction += await self.count_tokens(str(content), conf, model)
            elif msg["role"] == "user":
                messages_to_purge.add(idx)
                token_reduction += await self.count_tokens(msg["content"], conf, model)
            else:
                raise ValueError(f"Unknown role: {msg['role']}")

            # Check if we are under the max token limit
            if total_tokens_used - token_reduction <= max_tokens:
                break

        # Remove messages
        total_tokens_used -= token_reduction
        for idx in sorted(messages_to_purge, reverse=True):
            messages.pop(idx)

        # Check if we are under the max token limit
        if total_tokens_used <= max_tokens:
            log.info(f"First sweep successful for {user} (total: {total_tokens_used}/max: {max_tokens})")
            return True

        # If still not under the max token limit, we will now remove function calls available to model
        function_indexes_to_purge = set()
        token_reduction = 0
        for idx, func in enumerate(function_list):
            token_reduction += await self.count_tokens(json.dumps(func), conf, model)
            function_indexes_to_purge.add(idx)
            if total_tokens_used - token_reduction <= max_tokens:
                break

        # Remove function calls
        total_tokens_used -= token_reduction
        for idx in sorted(function_indexes_to_purge, reverse=True):
            function_list.pop(idx)

        # Check if we are under the max token limit
        if total_tokens_used <= max_tokens:
            log.info(f"Second sweep successful for {user} (total: {total_tokens_used}/max: {max_tokens})")
            return True

        # If still not under the max token limit, we will now DEGRADE the most recent user and assistant messages
        # We will also remove the most recent function/tool message if it exists
        messages_to_purge = set()
        token_reduction = 0
        # Just start degrading from the first onward
        for idx, msg in enumerate(messages):
            if msg["role"] == "system":
                continue
            # This message will get popped
            token_reduction += 4
            if msg["role"] in ["tool", "function"]:
                messages_to_purge.add(idx)
                token_reduction += await self.count_tokens(msg["content"], conf, model)
            elif msg["role"] == "assistant":
                messages_to_purge.add(idx)
                content = msg["content"] or msg.get("tool_calls", "") or msg.get("function_call", "")
                token_reduction += await self.count_tokens(str(content), conf, model)
            elif msg["role"] == "user":
                messages_to_purge.add(idx)
                token_reduction += await self.count_tokens(msg["content"], conf, model)
            else:
                raise ValueError(f"Unknown role: {msg['role']}")

            # Check if we are under the max token limit
            if total_tokens_used - token_reduction <= max_tokens:
                break

        # Remove messages
        total_tokens_used -= token_reduction
        for idx in sorted(messages_to_purge, reverse=True):
            messages.pop(idx)

        # Check if we destroyed the whole convo
        messages_without_system = sum(1 for msg in messages if msg["role"] != "system")
        if messages_without_system == 0:
            # We failed or the admins are trying their damn best to configure stupid settings
            raise ValueError(f"Failed to degrade conversation for {user}, guild owner needs to check settings")

        log.info(f"Third sweep successful for {user} (total: {total_tokens_used}/max: {max_tokens})")
        return True

    @perf()
    async def degrade_conversation_OLD(
        self,
        messages: List[dict],
        function_list: List[dict],
        conf: GuildSettings,
        user: Optional[discord.Member],
    ) -> Tuple[List[dict], List[dict], bool]:
        """
        Iteratively degrade a conversation payload, prioritizing more recent messages and critical context.

        Order of importance:
        - System messages
        - Most recent user message
        - Most recent assistant message
        - Most recent function/tool message

        Args:
            messages (List[dict]): message entries sent to the api
            function_list (List[dict]): list of json function schemas for the model
            conf: (GuildSettings): current settings

        Returns:
            Tuple[List[dict], List[dict], bool]: updated messages list, function list, and whether the conversation was degraded
        """
        messages = messages.copy()
        function_list = function_list.copy()

        def _degrade_message(msg: str) -> str:
            words = msg.split()
            if len(words) > 1:
                return " ".join(words[:-1])
            else:
                return ""

        model = conf.get_user_model(user)
        total_tokens = await self.count_payload_tokens(messages, conf, model)
        total_tokens += await self.count_function_tokens(function_list, conf, model)

        # Check if the total token count is already under the max token limit
        max_response_tokens = conf.get_user_max_response_tokens(user)
        max_tokens = self.get_max_tokens(conf, user)
        if max_tokens > max_response_tokens:
            max_tokens = max_tokens - max_response_tokens

        if total_tokens <= max_tokens:
            return messages, function_list, False

        # Find the indices of the most recent messages for each role
        most_recent_user = most_recent_function = most_recent_assistant = most_recent_tool = -1
        for i, msg in enumerate(reversed(messages)):
            if most_recent_user == -1 and msg["role"] == "user":
                most_recent_user = len(messages) - 1 - i
            if most_recent_function == -1 and msg["role"] == "function":
                most_recent_function = len(messages) - 1 - i
            if most_recent_tool == -1 and msg["role"] == "tool":
                most_recent_tool = len(messages) - 1 - i
            if most_recent_assistant == -1 and msg["role"] == "assistant":
                most_recent_assistant = len(messages) - 1 - i
            if most_recent_user != -1 and most_recent_function != -1 and most_recent_assistant != -1:
                break
            await asyncio.sleep(0.00001)

        # Clear out function calls (not the result, just the message of it being called)
        log.info(f"Degrading function calls for {user} (total: {total_tokens}/max: {max_tokens})")
        i = 0
        while total_tokens > max_tokens and i < len(messages):
            if messages[i]["content"] or messages[i].get("tool_calls"):
                i += 1
                continue
            messages.pop(i)
            total_tokens -= 5  # Minus role and name
            await asyncio.sleep(0.00001)

        if total_tokens <= max_tokens:
            return messages, function_list, True

        log.info(f"Degrading messages for {user} (total: {total_tokens}/max: {max_tokens})")
        # Degrade the conversation except for the most recent user, assistant, and function/tool messages
        i = 0
        while total_tokens > max_tokens and i < len(messages):
            if (
                messages[i]["role"] == "system"
                or i == most_recent_user
                or i == most_recent_function
                or i == most_recent_tool
                or i == most_recent_assistant
            ):
                i += 1
                continue

            if not messages[i]["content"]:
                if "function_call" not in messages[i]:
                    messages.pop(i)
                    total_tokens -= 5
                else:
                    i += 1
                continue

            if total_tokens <= max_tokens:
                return messages, function_list, True

            # Content is either a list or a string
            if isinstance(messages[i]["content"], list):
                for idx, msg in enumerate(messages[i]["content"]):
                    if msg["type"] != "text":
                        continue
                    degraded_content = _degrade_message(msg["text"])
                    pre = await self.count_tokens(msg["text"], conf, model)
                    post = await self.count_tokens(degraded_content, conf, model)
                    diff = pre - post
                    messages[i]["content"][idx]["text"] = degraded_content
                    total_tokens -= diff
                    await asyncio.sleep(0.00001)
            else:
                degraded_content = _degrade_message(messages[i]["content"])
                pre = await self.count_tokens(messages[i]["content"], conf, model)
                if degraded_content:
                    post = await self.count_tokens(degraded_content, conf, model)
                    diff = pre - post
                    messages[i]["content"] = degraded_content
                    total_tokens -= diff
                else:
                    total_tokens -= pre
                    total_tokens -= 4
                    messages.pop(i)

            if total_tokens <= max_tokens:
                return messages, function_list, True

            await asyncio.sleep(0.00001)

        # Wipe all tool call messages:
        i = 0
        while total_tokens > max_tokens and i < len(messages):
            if "tool_calls" not in messages[i] and messages[i]["role"] != "tool":
                i += 1
                continue
            messages.pop(i)
            await asyncio.sleep(0.00001)

        log.debug(f"Removing functions for {user} (total: {total_tokens}/max: {max_tokens})")
        # Degrade function_list before last resort
        while total_tokens > max_tokens and len(function_list) > 0:
            popped = function_list.pop(0)
            total_tokens -= await self.count_tokens(json.dumps(popped), conf, model)
            if total_tokens <= max_tokens:
                return messages, function_list, True
            await asyncio.sleep(0.00001)

        # Degrade the most recent user and function messages as the last resort
        log.debug(f"Degrading user/function messages for {user} (total: {total_tokens}/max: {max_tokens})")
        for i in [most_recent_function, most_recent_user, most_recent_tool]:
            if total_tokens <= max_tokens:
                return messages, function_list, True
            while total_tokens > max_tokens:
                if isinstance(messages[i]["content"], list):
                    for idx, msg in enumerate(messages[i]["content"]):
                        if msg["type"] != "text":
                            continue
                        degraded_content = _degrade_message(msg["text"])
                        pre = await self.count_tokens(msg["text"], conf, model)
                        post = await self.count_tokens(degraded_content, conf, model)
                        diff = pre - post
                        messages[i]["content"][idx]["text"] = degraded_content
                        total_tokens -= diff
                        await asyncio.sleep(0.00001)
                else:
                    degraded_content = _degrade_message(messages[i]["content"])
                    pre = await self.count_tokens(messages[i]["content"], conf, model)
                    if degraded_content:
                        post = await self.count_tokens(degraded_content, conf, model)
                        diff = pre - post
                        messages[i]["content"] = degraded_content
                        total_tokens -= diff
                    else:
                        total_tokens -= pre
                        total_tokens -= 4
                        messages.pop(i)
                await asyncio.sleep(0.00001)
        return messages, function_list, True

    async def token_pagify(self, text: str, conf: GuildSettings) -> List[str]:
        """Pagify a long string by tokens rather than characters"""
        if not text:
            log.debug("No text to pagify!")
            return []
        token_chunks = []
        tokens = await self.get_tokens(text, conf)
        current_chunk = []

        max_tokens = min(conf.max_tokens - 100, MODELS[conf.model])
        for token in tokens:
            current_chunk.append(token)
            if len(current_chunk) == max_tokens:
                token_chunks.append(current_chunk)
                current_chunk = []

        if current_chunk:
            token_chunks.append(current_chunk)

        text_chunks = []
        for chunk in token_chunks:
            text = await self.get_text(chunk)
            text_chunks.append(text)

        return text_chunks

    # -------------------------------------------------------
    # -------------------------------------------------------
    # ----------------------- EMBEDS ------------------------
    # -------------------------------------------------------
    # -------------------------------------------------------
    async def get_function_menu_embeds(self, user: discord.Member) -> List[discord.Embed]:
        func_dump = {k: v.model_dump() for k, v in self.db.functions.items()}
        registry = {"Assistant-Custom": func_dump}
        for cog_name, function_schemas in self.registry.items():
            cog = self.bot.get_cog(cog_name)
            if not cog:
                continue
            for function_name, function_schema in function_schemas.items():
                function_obj = getattr(cog, function_name, None)
                if function_obj is None:
                    continue
                if cog_name not in registry:
                    registry[cog_name] = {}
                registry[cog_name][function_name] = {
                    "code": inspect.getsource(function_obj),
                    "jsonschema": function_schema,
                }

        conf = self.db.get_conf(user.guild)
        model = conf.get_user_model(user)

        pages = sum(len(v) for v in registry.values())
        page = 1
        embeds = []
        for cog_name, functions in registry.items():
            for function_name, func in functions.items():
                embed = discord.Embed(
                    title=_("Custom Functions"),
                    description=function_name,
                    color=discord.Color.blue(),
                )
                if cog_name != "Assistant-Custom":
                    embed.add_field(
                        name=_("3rd Party"),
                        value=_("This function is managed by the `{}` cog").format(cog_name),
                        inline=False,
                    )
                elif cog_name == "Assistant":
                    embed.add_field(
                        name=_("Internal Function"),
                        value=_("This is an internal command that can only be used when interacting with a tutor"),
                        inline=False,
                    )
                schema = json.dumps(func["jsonschema"], indent=2)
                tokens = await self.count_tokens(schema, conf, model)

                schema_text = _("This function consumes `{}` input tokens each call\n").format(humanize_number(tokens))

                if user.id in self.bot.owner_ids:
                    if len(schema) > 900:
                        schema_text += box(schema[:900] + "...", "py")
                    else:
                        schema_text += box(schema, "py")

                    if len(func["code"]) > 900:
                        code_text = box(func["code"][:900] + "...", "py")
                    else:
                        code_text = box(func["code"], "py")

                else:
                    schema_text += box(func["jsonschema"]["description"], "json")
                    code_text = box(_("Hidden..."))

                embed.add_field(name=_("Schema"), value=schema_text, inline=False)
                embed.add_field(name=_("Code"), value=code_text, inline=False)

                embed.set_footer(text=_("Page {}/{}").format(page, pages))
                embeds.append(embed)
                page += 1

        if not embeds:
            embeds.append(
                discord.Embed(
                    description=_("No custom code has been added yet!"),
                    color=discord.Color.purple(),
                )
            )
        return embeds

    async def get_embbedding_menu_embeds(self, conf: GuildSettings, place: int) -> List[discord.Embed]:
        embeddings = sorted(conf.embeddings.items(), key=lambda x: x[0])
        embeds = []
        pages = math.ceil(len(embeddings) / 5)
        model = conf.get_user_model()
        start = 0
        stop = 5
        for page in range(pages):
            stop = min(stop, len(embeddings))
            embed = discord.Embed(title=_("Embeddings"), color=discord.Color.blue())
            embed.set_footer(text=_("Page {}/{}").format(page + 1, pages))
            num = 0
            for i in range(start, stop):
                name, embedding = embeddings[i]
                tokens = await self.count_tokens(embedding.text, conf, model)
                text = (
                    box(f"{embedding.text[:30].strip()}...")
                    if len(embedding.text) > 33
                    else box(embedding.text.strip())
                )
                val = _(
                    "`Created:    `{}\n"
                    "`Modified:   `{}\n"
                    "`Tokens:     `{}\n"
                    "`Dimensions: `{}\n"
                    "`AI Created: `{}\n"
                ).format(
                    embedding.created_at(),
                    embedding.modified_at(relative=True),
                    tokens,
                    len(embedding.embedding),
                    embedding.ai_created,
                )
                val += text
                fieldname = f"➣ {name}" if place == num else name
                embed.add_field(
                    name=fieldname[:250],
                    value=val,
                    inline=False,
                )
                num += 1
            embeds.append(embed)
            start += 5
            stop += 5
        if not embeds:
            embeds.append(discord.Embed(description=_("No embeddings have been added!"), color=discord.Color.purple()))
        return embeds
