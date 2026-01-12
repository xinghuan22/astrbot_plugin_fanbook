import io
import math

import numpy as np
from PIL import Image

from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .hex import ImageWorkflow


class FanqieHex(ImageWorkflow):
    def __init__(self):
        super().__init__()

    def process(self, message_event: AstrMessageEvent, mode: str) -> bytes | None:
        image_bytes = self.get_first_image(message_event)
        if isinstance(image_bytes, bytes):
            img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
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

            elif mode == "decrypt":
                # 解混淆: 将路径上的像素序列向左循环移动 offset (还原)
                shifted_pixels = np.roll(path_pixels, -offset, axis=0)
                # 填回
                result_flat[indices_in_path] = shifted_pixels

            # 重组图片
            res_img = Image.fromarray(result_flat.reshape(height, width, 4))

            # 导出
            output_buffer = io.BytesIO()
            # 建议使用 PNG 以避免 JPEG 噪点导致无法完美还原
            # 如果必须模拟原站的有损压缩，改用 format="JPEG", quality=95
            res_img.save(output_buffer, format="PNG")
            return output_buffer.getvalue()

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
