import os
import time
import sys

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, MessageChain
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp
from astrbot.api import logger
from .jm import JmDownload

@register("astrbot_plugin_fanbook", "xinghuan22", "一个简单的 下载JM等本子的插件", "1.0.0")
class MyPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.path: str = os.path.join("data", "plugins_data", "astrbot_plugin_fanbook")
        os.makedirs(self.path, exist_ok=True)

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""
    
    @filter.command("jm")
    async def jm(self, event: AstrMessageEvent):
        user_name = event.get_sender_name()
        message_str = event.message_str # 用户发的纯文本消息字符串
        message_chain = event.get_messages() # 用户所发的消息的消息链 # from astrbot.api.message_components import *
        logger.info(message_chain)
        
        # 记录开始时间
        start_time = time.time()
        yield event.plain_result(f"Hello, {user_name}, 正在初始化下载任务，请稍等...") # 发送一条纯文本消息
        
        # 定义进度回调函数（同步方式）
        def progress_callback(file_count, estimated_str):
            import asyncio, threading
            # 直接在新线程中运行异步函数，避免RuntimeWarning
            def run_async():
                async def send_progress():
                    await self.context.send_message(event.unified_msg_origin, MessageChain().message(f"下载已完成，共{file_count}张图片，预计PDF制作需要: {estimated_str}，请耐心等待..."))
                # 运行异步函数
                asyncio.run(send_progress())
            
            # 在新线程中运行异步函数
            thread = threading.Thread(target=run_async)
            thread.start()
            
        # 调用下载函数并获取PDF文件路径和文件数量
        try:
            # 获取op.yml配置文件的路径
            config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'op.yml')
            pdf_path, file_count = JmDownload(message_str, self.path, config_path, progress_callback)
            
            # 计算总耗时
            elapsed_time = time.time() - start_time
            time_str = f"{elapsed_time:.2f}秒"
            
            # 检查PDF文件是否存在
            if pdf_path and os.path.exists(pdf_path):
                # 发送完成消息和实际耗时
                yield event.plain_result(f"已完成！共{file_count}张图片，实际耗时: {time_str}，文件发送中。。。")
                
                # 发送PDF文件回bot
                chain = [
                    Comp.File(file=pdf_path, name=os.path.basename(pdf_path)) 
                ]
                yield event.chain_result(chain)
            else:
                yield event.plain_result(f"下载失败: 无法生成PDF文件，耗时: {time_str}")
        except Exception as e:
            # 计算总耗时
            elapsed_time = time.time() - start_time
            time_str = f"{elapsed_time:.2f}秒"
            logger.error(f"下载失败: {str(e)}")
            yield event.plain_result(f"下载失败: {str(e)}，耗时: {time_str}")

    # 注册指令的装饰器。指令名为 helloworld。注册成功后，发送 `/jm helloworld` 就会触发这个指令，并回复 `你好, {user_name}!`
    @filter.command("helloworld")
    async def helloworld(self, event: AstrMessageEvent):
        """这是一个 hello world 指令""" # 这是 handler 的描述，将会被解析方便用户了解插件内容。建议填写。
        user_name = event.get_sender_name()
        message_str = event.message_str # 用户发的纯文本消息字符串
        message_chain = event.get_messages() # 用户所发的消息的消息链 # from astrbot.api.message_components import *
        logger.info(message_chain)
        yield event.plain_result(f"Hello, {user_name}, 你发了 {message_str}!") # 发送一条纯文本消息

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
