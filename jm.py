from calendar import c
from jmcomic import download_album, create_option_by_file
import os
from astrbot.core.star.context import Context
from astrbot.api.event import MessageChain
import img2pdf
import shutil

def webp_to_pdf(folder_name, output_pdf):
    # 获取所有webp图片并按名称排序
    try:
        files = [f for f in os.listdir(folder_name) if f.lower().endswith('.webp')]
        files.sort()
        file_paths = [os.path.join(folder_name, f) for f in files]

        # 合并为PDF
        with open(output_pdf, "wb") as f:
            f.write(img2pdf.convert(file_paths))
            
        # 返回文件数量
        return len(files)
    except Exception as e:
        raise Exception(f"转换PDF时出错: {str(e)}")
        
def cb(album, downloader, save_path, progress_callback):
    try:
        # 获取下载目录中的文件夹名称
        download_folder = os.path.join(save_path, album.name)
        
        # 计算文件数量用于预估转换时间
        file_count = 0
        if os.path.exists(download_folder):
            file_count = len([f for f in os.listdir(download_folder) if f.lower().endswith('.webp')])
        
        # 如果提供了进度回调函数，发送预估时间给用户
        # 预估PDF制作时间（假设每张图片需要0.5秒处理时间）
        estimated_time = file_count * 0.5
        estimated_str = f"{estimated_time:.1f}秒"
        # 使用同步方式调用回调函数
        progress_callback(file_count, estimated_str)
        
        # 使用id_name的形式命名PDF文件，但需要处理特殊字符
        # 移除或替换文件名中的特殊字符
        pdfname = f"{album.id}_{album.name}.pdf"
        pdf_path = os.path.join(save_path, pdfname)
        
        # 确保保存目录存在
        os.makedirs(save_path, exist_ok=True)
        
        # 转换webp图片为PDF，并获取文件数量
        if not os.path.exists(pdf_path):
            webp_to_pdf(folder_name=download_folder, output_pdf=pdf_path)
        
        # 删除原始图片目录
        # shutil.rmtree(album.name)
        
        # 返回PDF文件路径和文件数量
        return pdf_path, file_count
    except Exception as e:
        raise Exception(f"处理PDF文件时出错: {str(e)}")
    
def JmDownload(album_id: str, save_path: str, config_path: str = None, progress_callback=None):
    # 如果没有提供配置文件路径，则使用默认路径
    if config_path is None:
        # 获取当前脚本所在目录
        current_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(current_dir, 'op.yml')
    
    option = create_option_by_file(config_path)
    # 更新下载目录为指定的保存路径
    option.dir_rule.base_dir = save_path
    
    # 用于存储PDF路径和文件数量的变量
    pdf_path_result = None
    file_count_result = 0
    
    # 定义回调函数
    def download_callback(album, downloader):
        nonlocal pdf_path_result, file_count_result
        pdf_path_result, file_count_result = cb(album, downloader, save_path, progress_callback)
    
    # 执行下载
    download_album(jm_album_id=album_id, option=option, callback=download_callback)
    
    # 等待下载完成（简单等待一段时间，直到结果准备好）
    import time
    start_time = time.time()
    while pdf_path_result is None and time.time() - start_time < 120:  # 最多等待2分钟
        time.sleep(0.1)
    
    # 返回PDF文件路径和文件数量
    return pdf_path_result, file_count_result