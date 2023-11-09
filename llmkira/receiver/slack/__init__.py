# -*- coding: utf-8 -*-
# @Time    : 2023/10/21 下午6:25
# @Author  : sudoskys
# @File    : __init__.py.py
# @Software: PyCharm
import ssl
from typing import List

from loguru import logger
from slack_sdk.web.async_client import AsyncWebClient
from telebot import formatting

from llmkira.middleware.env_virtual import EnvManager
from llmkira.middleware.llm_task import OpenaiMiddleware
from llmkira.receiver import function
from llmkira.receiver.receiver_client import BaseReceiver, BaseSender
from llmkira.receiver.slack.creat_message import ChatMessageCreator
from llmkira.schema import RawMessage
from llmkira.sdk.endpoint import openai
from llmkira.sdk.func_calling.register import ToolRegister
from llmkira.sdk.schema import File, Message, FunctionCall
from llmkira.sdk.utils import sync
from llmkira.setting.slack import BotSetting
from llmkira.task import Task, TaskHeader

__receiver__ = "slack"

from llmkira.middleware.router.schema import router_set

# 魔法
import nest_asyncio

nest_asyncio.apply()
# 设置路由系统
router_set(role="receiver", name=__receiver__)


class SlackSender(BaseSender):
    """
    平台路由
    """

    def __init__(self):
        if not BotSetting.available:
            return
        ssl_cert = ssl.SSLContext()
        if BotSetting.proxy_address:
            self.proxy = BotSetting.proxy_address
            logger.info("SlackBot proxy_tunnels being used in `AsyncWebClient`!")
        self.bot = AsyncWebClient(
            token=BotSetting.bot_token,
            ssl=ssl_cert,
            proxy=BotSetting.proxy_address
        )

    async def file_forward(self, receiver: TaskHeader.Location, file_list: List[File], **kwargs):
        for file_obj in file_list:
            # URL FIRST
            if file_obj.file_url:
                await self.bot.files_upload_v2(
                    file=file_obj.file_url,
                    channels=receiver.chat_id,
                    thread_ts=receiver.message_id,
                    filename=file_obj.file_name,
                )
            # DATA
            _data: File.Data = sync(File.download_file(file_obj.file_id))
            if not _data:
                logger.error(f"file download failed {file_obj.file_id}")
                continue
            try:
                await self.bot.files_upload_v2(
                    file=_data.file_data,
                    filename=_data.file_name,
                    channels=receiver.chat_id,
                    thread_ts=receiver.message_id,
                )
            except Exception as e:
                logger.error(e)
                logger.error(f"file upload failed {file_obj.file_id}")
                await self.bot.chat_postMessage(
                    channel=receiver.chat_id,
                    thread_ts=receiver.message_id,
                    text=f"Failed,Server down,or bot do not have *Bot Token Scopes* of `files:write` scope to upload file."
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
            _message = ChatMessageCreator(
                channel=receiver.chat_id,
                thread_ts=receiver.message_id
            ).update_content(message_text=item.text).get_message_payload(message_text=item.text)
            await self.bot.chat_postMessage(
                **_message
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
            _message = ChatMessageCreator(
                channel=receiver.chat_id,
                thread_ts=receiver.message_id
            ).update_content(message_text=item.content).get_message_payload(message_text=item.content)
            await self.bot.chat_postMessage(
                channel=receiver.chat_id,
                thread_ts=receiver.message_id,
                text=item.content
            )

    async def error(self, receiver: TaskHeader.Location, text, **kwargs):
        _message = ChatMessageCreator(
            channel=receiver.chat_id,
            thread_ts=receiver.message_id
        ).update_content(message_text=text).get_message_payload(message_text=text)
        await self.bot.chat_postMessage(
            **_message
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
                formatting.mbold("🦴 Task be created:") + f" `{function_call.name}` ",
                f"""```\n{function_call.arguments}```""",
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


__sender__ = SlackSender()


class SlackReceiver(BaseReceiver):
    """
    receive message from telegram
    """

    async def slack(self):
        self.set_core(sender=__sender__, task=Task(queue=__receiver__))
        if not BotSetting.available:
            logger.warning("Receiver Runtime:Slack Setting empty")
            return None
        try:
            logger.success("Receiver Runtime:Slack start")
            await self.task.consuming_task(self.on_message)
        except KeyboardInterrupt:
            logger.warning("Slack Receiver shutdown")
        except Exception as e:
            logger.exception(e)
            raise e
