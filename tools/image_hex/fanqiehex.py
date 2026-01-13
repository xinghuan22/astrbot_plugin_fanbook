import io
import math

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from astrbot.api import logger
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .hex import ImageWorkflow


class FanqieHex(ImageWorkflow):
    def __init__(self):
        super().__init__(proxy="http://10.17.196.164:20172")

    async def process(self, message_event: AstrMessageEvent, mode: str) -> bytes | None:
        logger.info(
            f"[fanqiehex] process message_event: {message_event.message_obj.message}"
        )
        image_bytes = await self.get_first_image(message_event)
        if isinstance(image_bytes, bytes):
            img = Image.open(io.BytesIO(initial_bytes=image_bytes)).convert("RGBA")
            width, height = img.size
            img_arr = np.array(img)

            # 获取 Gilbert 路径坐标
            print(f"Generating curve for {width}x{height}...")
            curve_coords = self.gilbert2d(width, height)

            # 将二维坐标转为一维索引 (Flat Index)
            # y * width + x
            flat_indices = np.array([y * width + x for x, y in curve_coords])

            # 计算偏移量 (黄金分割)
            offset = int(round((math.sqrt(5) - 1) / 2 * width * height))

            # 准备像素数据
            pixels_flat = img_arr.reshape(-1, 4)
            result_flat = np.zeros_like(pixels_flat)

            # -------------------------------------------------
            # 核心算法逻辑
            # -------------------------------------------------
            # indices_in_path[k] 表示 Gilbert 路径上第 k 步对应的原图像素索引
            indices_in_path = flat_indices

            # 提取按路径排列的像素序列
            path_pixels = pixels_flat[indices_in_path]

            if mode == "encrypt":
                # 混淆: 将路径上的像素序列向右循环移动 offset
                shifted_pixels = np.roll(path_pixels, offset, axis=0)
                # 将移动后的像素填回原图物理位置
                result_flat[indices_in_path] = shifted_pixels
                res_img = Image.fromarray(result_flat.reshape(height, width, 4))

            elif mode == "decrypt":
                # 解混淆: 将路径上的像素序列向左循环移动 offset (还原)
                shifted_pixels = np.roll(path_pixels, -offset, axis=0)
                # 填回
                result_flat[indices_in_path] = shifted_pixels
                res_img = Image.fromarray(result_flat.reshape(height, width, 4))
                logger.info("添加水印...")
                import secrets

                ramtxt = secrets.token_hex(10)
                res_img = self.add_watermark(res_img, text=ramtxt)

            # 导出
            output_buffer = io.BytesIO()
            # 建议使用 PNG 以避免 JPEG 噪点导致无法完美还原
            # 如果必须模拟原站的有损压缩，改用 format="JPEG", quality=95
            res_img.save(output_buffer, format="PNG")
            return output_buffer.getvalue()
        return None

    def generate2d(self, x, y, ax, ay, bx, by, coordinates):
        """
        递归生成广义希尔伯特曲线坐标
        """
        w = abs(ax + ay)
        h = abs(bx + by)

        dax = (ax > 0) - (ax < 0)
        day = (ay > 0) - (ay < 0)
        dbx = (bx > 0) - (bx < 0)
        dby = (by > 0) - (by < 0)

        if h == 1:
            for i in range(w):
                coordinates.append((x, y))
                x += dax
                y += day
            return

        if w == 1:
            for i in range(h):
                coordinates.append((x, y))
                x += dbx
                y += dby
            return

        ax2 = ax // 2
        ay2 = ay // 2
        bx2 = bx // 2
        by2 = by // 2

        w2 = abs(ax2 + ay2)
        h2 = abs(bx2 + by2)

        if 2 * w > 3 * h:
            if (w2 % 2) and (w > 2):
                ax2 += dax
                ay2 += day

            self.generate2d(x, y, ax2, ay2, bx, by, coordinates)
            self.generate2d(x + ax2, y + ay2, ax - ax2, ay - ay2, bx, by, coordinates)

        else:
            if (h2 % 2) and (h > 2):
                bx2 += dbx
                by2 += dby

            self.generate2d(x, y, bx2, by2, ax2, ay2, coordinates)
            self.generate2d(x + bx2, y + by2, ax, ay, bx - bx2, by - by2, coordinates)
            self.generate2d(
                x + (ax - dax) + (bx2 - dbx),
                y + (ay - day) + (by2 - dby),
                -bx2,
                -by2,
                -(ax - ax2),
                -(ay - ay2),
                coordinates,
            )

    def gilbert2d(self, width, height):
        """
        入口函数
        """
        coordinates = []
        if width >= height:
            self.generate2d(0, 0, width, 0, 0, height, coordinates)
        else:
            self.generate2d(0, 0, 0, height, width, 0, coordinates)
        return coordinates

    def add_watermark(self, img, text="kissnab"):
        """
        在图片右下角添加与背景色相近的微小水印
        """
        draw = ImageDraw.Draw(img)
        width, height = img.size

        # 1. 设置字体大小 (自适应图片高度，很小)
        # 大约占图片高度的 1.5% 到 2%，最小 10px
        font_size = max(10, int(height * 0.01))

        try:
            # 尝试加载常用字体，如果没有则使用默认字体
            # Windows/Linux 路径可能不同，这里尝试加载 Arial
            font = ImageFont.truetype("arial.ttf", font_size)
        except OSError:
            font = ImageFont.load_default(size=font_size)

        # 2. 计算文字宽高
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

        # 3. 确定位置 (右下角，留出少量边距)
        margin = 5
        x = width - text_w - margin
        y = height - text_h - margin

        # 边界检查，防止图片太小文字出界
        if x < 0:
            x = 0
        if y < 0:
            y = 0

        # 4. 采样背景颜色以计算"相近色"
        # 获取文字区域中心点的颜色
        sample_x = min(width - 1, int(x + text_w / 2))
        sample_y = min(height - 1, int(y + text_h / 2))

        bg_color = img.getpixel((sample_x, sample_y))

        # 提取 RGB
        if isinstance(bg_color, int):  # 灰度图
            r = g = b = bg_color
            a = 255
        elif len(bg_color) == 4:  # RGBA
            r, g, b, a = bg_color
        else:  # RGB
            r, g, b = bg_color
            a = 255

        # 计算亮度 (Luminance)
        luminance = 0.299 * r + 0.587 * g + 0.114 * b

        # 5. 生成水印颜色
        # 策略：如果背景亮，文字就稍微暗一点；如果背景暗，文字就稍微亮一点
        # delta 控制色差大小，值越小越"隐形"
        delta = 5

        if luminance > 128:
            # 背景亮 -> 文字微暗
            new_r = max(0, r - delta)
            new_g = max(0, g - delta)
            new_b = max(0, b - delta)
        else:
            # 背景暗 -> 文字微亮
            new_r = min(255, r + delta)
            new_g = min(255, g + delta)
            new_b = min(255, b + delta)

        text_color = (new_r, new_g, new_b, int(a * 0.9))  # 稍微加点透明度融合更好

        # 6. 绘制文字
        draw.text((x, y), text, font=font, fill=text_color)

        return img
