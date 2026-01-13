import os
import time

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

from .jm import JmDownload, jmToph
from .tools.image_hex.fanqiehex import FanqieHex


@register(
    "astrbot_plugin_fanbook", "xinghuan22", "一个简单的 下载JM等本子的插件", "1.0.0"
)
class MyPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.path: str = os.path.join("data", "plugins_data", "astrbot_plugin_fanbook")
        os.makedirs(self.path, exist_ok=True)

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""

    @filter.command("jm")
    async def jm(self, event: AstrMessageEvent):
        # user_name = event.get_sender_name()
        message_str = event.message_str  # 用户发的纯文本消息字符串
        message_chain = (
            event.get_messages()
        )  # 用户所发的消息的消息链 # from astrbot.api.message_components import *
        logger.info(message_chain)
        logger.info(message_str)

        # 去除消息开头的command前缀
        if message_str.startswith("jm"):
            message_str = message_str[2:].strip()
        # 根据空格分隔消息
        send_type = "url"
        message_strs = message_str.split(" ")
        if len(message_strs) < 1:
            yield event.plain_result("未找到有效本子id。")  # 发送一条纯文本消息
            return
        message_str = message_strs[0]
        if len(message_strs) >= 2 and message_strs[1] in ["file", "url"]:
            send_type = message_strs[1]
        # 解析消息是不是纯数字
        if not message_str.isdigit():
            yield event.plain_result("id不是纯数字呢！")  # 发送一条纯文本消息
            return

        # 记录开始时间
        start_time = time.time()
        match send_type:
            case "file":
                yield event.plain_result("开始下载本子文件")  # 发送一条纯文本消息

                # 定义进度回调函数（同步方式）
                def progress_callback(file_count, estimated_str):
                    import asyncio
                    import threading

                    # 直接在新线程中运行异步函数，避免RuntimeWarning
                    def run_async():
                        async def send_progress():
                            await self.context.send_message(
                                event.unified_msg_origin,
                                MessageChain().message(
                                    f"下载已完成，共{file_count}张图片，预计PDF制作需要: {estimated_str}，请耐心等待..."
                                ),
                            )

                        # 运行异步函数
                        asyncio.run(send_progress())

                    # 在新线程中运行异步函数
                    thread = threading.Thread(target=run_async)
                    thread.start()

                # 调用下载函数并获取PDF文件路径和文件数量
                try:
                    # 获取op.yml配置文件的路径
                    config_path = os.path.join(
                        os.path.dirname(os.path.abspath(__file__)), "op.yml"
                    )
                    pdf_path, file_count = JmDownload(
                        message_str, self.path, config_path, progress_callback
                    )

                    # 计算总耗时
                    elapsed_time = time.time() - start_time
                    time_str = f"{elapsed_time:.2f}秒"

                    # 检查PDF文件是否存在
                    if pdf_path and os.path.exists(pdf_path):
                        # 发送完成消息和实际耗时
                        yield event.plain_result(
                            f"已完成！共{file_count}张图片，实际耗时: {time_str}，文件发送中。。。"
                        )

                        # 发送PDF文件回bot
                        chain: list[Comp.BaseMessageComponent] = [
                            Comp.File(file=pdf_path, name=os.path.basename(pdf_path))
                        ]
                        yield event.chain_result(chain)
                    else:
                        yield event.plain_result(
                            f"下载失败: 无法生成PDF文件，耗时: {time_str}"
                        )
                except Exception as e:
                    # 计算总耗时
                    elapsed_time = time.time() - start_time
                    time_str = f"{elapsed_time:.2f}秒"
                    logger.error(f"下载失败: {str(e)}")
                    yield event.plain_result(f"下载失败: {str(e)}，耗时: {time_str}")
            case "url":
                # 获取op.yml配置文件的路径
                config_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), "op.yml"
                )
                result = await jmToph(message_str, config_path)
                node_list = []
                for res in result:
                    node_list.append(
                        Comp.Node(
                            uin="0",
                            name="jm",
                            content=[Comp.Plain(text=res)],
                        )
                    )
                yield event.chain_result([Comp.Nodes(nodes=node_list)])

        event.stop_event()

    # 注册指令的装饰器。指令名为 helloworld。注册成功后，发送 `/jm helloworld` 就会触发这个指令，并回复 `你好, {user_name}!`
    @filter.command("helloworld")
    async def helloworld(self, event: AstrMessageEvent):
        """这是一个 hello world 指令"""  # 这是 handler 的描述，将会被解析方便用户了解插件内容。建议填写。
        user_name = event.get_sender_name()
        message_str = event.message_str  # 用户发的纯文本消息字符串
        message_chain = (
            event.get_messages()
        )  # 用户所发的消息的消息链 # from astrbot.api.message_components import *
        logger.info(message_chain)
        yield event.plain_result(
            f"Hello, {user_name}, 你发了 {message_str}!"
        )  # 发送一条纯文本消息

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""

    # 注册指令的装饰器。指令名为 helloworld。注册成功后，发送 `/jm helloworld` 就会触发这个指令，并回复 `你好, {user_name}!`
    @filter.regex(r"^(番茄混淆)", priority=5)
    async def fanqie_encrypt(
        self, event: AstrMessageEvent
    ):  # 这是 handler 的描述，将会被解析方便用户了解插件内容。建议填写。
        hex = FanqieHex()
        img = await FanqieHex.process(hex, event, "encrypt")
        if img:
            yield event.chain_result(
                [
                    Comp.Nodes(
                        [
                            Comp.Node(
                                uin="0",
                                name="hex",
                                content=[Comp.Image.fromBytes(byte=img)],
                            )
                        ]
                    )
                ]
            )
        else:
            yield event.chain_result([Comp.Plain("未找到图片。")])

        event.stop_event()

    @filter.regex(r"^(番茄解混淆)", priority=5)
    async def decrypt(self, event: AstrMessageEvent):
        logger.info("开始解析图片")
        hex = FanqieHex()
        img = await FanqieHex.process(hex, event, "decrypt")
        # logger.info(f"图片 {img}")
        if img:
            yield event.chain_result(
                [
                    Comp.Nodes(
                        [
                            Comp.Node(
                                uin="0",
                                name="hex",
                                content=[Comp.Image.fromBytes(byte=img)],
                            )
                        ]
                    )
                ]
            )
        else:
            yield event.chain_result([Comp.Plain("未找到图片。")])

        event.stop_event()
