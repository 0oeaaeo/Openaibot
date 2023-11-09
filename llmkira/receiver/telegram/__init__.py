# -*- coding: utf-8 -*-
# @Time    : 2023/8/17 下午8:46
# @Author  : sudoskys
# @File    : __init__.py.py
# @Software: PyCharm
import time
from typing import List

import telebot
from loguru import logger
from telebot import TeleBot, formatting

from llmkira.middleware.env_virtual import EnvManager
from llmkira.middleware.llm_task import OpenaiMiddleware
from llmkira.receiver import function
from llmkira.receiver.receiver_client import BaseReceiver, BaseSender
from llmkira.schema import RawMessage
from llmkira.sdk.endpoint import openai
from llmkira.sdk.func_calling.register import ToolRegister
from llmkira.sdk.schema import Message, File, FunctionCall
from llmkira.setting.telegram import BotSetting
from llmkira.task import Task, TaskHeader
from llmkira.sdk.utils import sync

__receiver__ = "telegram"

from llmkira.middleware.router.schema import router_set

router_set(role="receiver", name=__receiver__)


class TelegramSender(BaseSender):
    """
    平台路由
    """

    def __init__(self):
        if not BotSetting.available:
            return
        self.bot = TeleBot(token=BotSetting.token)
        from telebot import apihelper
        if BotSetting.proxy_address:
            apihelper.proxy = {'https': BotSetting.proxy_address}
        else:
            apihelper.proxy = None

    async def file_forward(self, receiver: TaskHeader.Location, file_list: List[File], **kwargs):
        for file_obj in file_list:
            if file_obj.file_url:
                self.bot.send_document(chat_id=receiver.chat_id, document=file_obj.file_url,
                                       reply_to_message_id=receiver.message_id, caption=file_obj.file_name)
                continue
            _data: File.Data = sync(File.download_file(file_obj.file_id))
            if not _data:
                logger.error(f"file download failed {file_obj.file_id}")
                continue
            if file_obj.file_name.endswith((".jpg", ".png", ".jpeg", ".gif")):
                self.bot.send_photo(
                    chat_id=receiver.chat_id,
                    photo=_data.pair,
                    reply_to_message_id=receiver.message_id,
                    caption=file_obj.caption
                )
            elif file_obj.file_name.endswith(".webp"):
                self.bot.send_sticker(
                    chat_id=receiver.chat_id,
                    reply_to_message_id=receiver.message_id,
                    sticker=_data.pair,
                )
            elif file_obj.file_name.endswith(".ogg"):
                self.bot.send_voice(
                    chat_id=receiver.chat_id,
                    voice=_data.pair,
                    reply_to_message_id=receiver.message_id,
                    caption=file_obj.caption
                )
            else:
                self.bot.send_document(
                    chat_id=receiver.chat_id,
                    document=_data.pair,
                    reply_to_message_id=receiver.message_id,
                    caption=file_obj.caption
                )

    async def forward(self, receiver: TaskHeader.Location, message: List[RawMessage], **kwargs):
        """
        插件专用转发，是Task通用类型
        """
        for item in message:
            await self.file_forward(
                receiver=receiver,
                file_list=item.file
            )
            if item.only_send_file:
                return None
            try:
                self.bot.send_message(
                    chat_id=receiver.chat_id,
                    text=item.text,
                    reply_to_message_id=receiver.message_id,
                    parse_mode="MarkdownV2"
                )
            except telebot.apihelper.ApiTelegramException as e:
                time.sleep(3)
                logger.error(f"telegram send message error, retry\n{e}")
                self.bot.send_message(
                    chat_id=receiver.chat_id,
                    text=item.text,
                    reply_to_message_id=receiver.message_id
                )

    async def reply(self, receiver: TaskHeader.Location, message: List[Message], **kwargs):
        """
        模型直转发，Message是Openai的类型
        """
        for item in message:
            raw_message = await self.loop_turn_from_openai(platform_name=__receiver__, message=item, locate=receiver)

            await self.file_forward(
                receiver=receiver,
                file_list=raw_message.file
            )
            if raw_message.only_send_file:
                return None
            assert item.content, f"message content is empty"
            self.bot.send_message(
                chat_id=receiver.chat_id,
                text=item.content,
                reply_to_message_id=receiver.message_id
            )

    async def error(self, receiver: TaskHeader.Location, text, **kwargs):
        self.bot.send_message(
            chat_id=receiver.chat_id,
            text=text,
            reply_to_message_id=receiver.message_id
        )

    async def function(self,
                       receiver: TaskHeader.Location,
                       task: TaskHeader,
                       llm: OpenaiMiddleware,
                       llm_result: openai.OpenaiResult,  # TODO Change to base result
                       function_class: str,
                       function_call_list: List[FunctionCall] = None,
                       ):
        async def loop_function_call(_function_call: FunctionCall = None):
            """
            单个处理函数
            """
            if not isinstance(_function_call, FunctionCall):
                logger.exception(f"function_call type error {type(_function_call)}")
                return None
            # assert isinstance(_function_call, FunctionCall), "input is not a function_call"
            if not _function_call:
                raise ValueError("message not have function_call,forward type error")
            # 获取设置查看是否静音
            _tool = ToolRegister().get_tool(_function_call.name)
            if not _tool:
                logger.warning(f"not found function {_function_call.name}")
                return None
            tool = _tool()
            _func_tips = [
                formatting.mbold("🦴 Task be created:") + f" `{_function_call.name}` ",
                formatting.mcode(_function_call.arguments),
            ]
            if tool.env_required:
                __secret__ = await EnvManager.from_uid(
                    uid=task.receiver.uid
                ).get_env_list(name_list=tool.env_required)
                # 查找是否有空
                _required_env = [
                    name
                    for name in tool.env_required
                    if not __secret__.get(name, None)
                ]
                _need_env_list = [
                    f"`{formatting.escape_markdown(name)}`"
                    for name in _required_env
                ]
                _need_env_str = ",".join(_need_env_list)
                _func_tips.append(formatting.mbold("🦴 Env required:") + f" {_need_env_str} ")
                help_docs = tool.env_help_docs(_required_env)
                _func_tips.append(formatting.mitalic(help_docs))
            task_message = formatting.format_text(
                *_func_tips,
                separator="\n"
            )

            if not tool.silent:
                await self.forward(
                    receiver=receiver,
                    message=[
                        RawMessage(
                            text=task_message,
                            only_send_file=False
                        )
                    ]
                )

        new_receiver = task.receiver.copy()
        new_receiver.platform = __receiver__
        """更新接收者为当前平台"""
        for index, function_call in enumerate(function_call_list):
            await loop_function_call(function_call)
            """处理批次函数"""
            new_meta = task.task_meta.pack_loop(
                loop_class=function_class,
                loop_index=int(index + 1),
                loop_length=len(function_call_list)
            )
            """克隆元数据为当前平台"""
            await Task(queue=function.__receiver__).send_task(
                task=TaskHeader.from_function(
                    parent_call=llm_result,
                    task_meta=new_meta,
                    receiver=new_receiver,
                    message=task.message
                )
            )
            """发送打包后的任务数据"""


__sender__ = TelegramSender()


class TelegramReceiver(BaseReceiver):
    """
    receive message from telegram
    """

    async def telegram(self):
        self.set_core(sender=__sender__, task=Task(queue=__receiver__))
        if not BotSetting.available:
            logger.warning("Receiver Runtime:TelegramBot Setting empty")
            return None
        logger.success("Receiver Runtime:TelegramBot start")
        await self.task.consuming_task(self.on_message)
