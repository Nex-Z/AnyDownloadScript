import base64
import os
import subprocess
from asyncio import Future
from concurrent.futures import ThreadPoolExecutor
from typing import Dict

from loguru import logger

from config.config import settings

# 可配置线程池大小
download_executor = ThreadPoolExecutor(max_workers = settings.download_works)

# 保存任务状态
download_tasks: Dict[str, Future] = {}


def save_base64_image(base64_str, file_name):
    # 去除前缀
    if base64_str.startswith('data:image'):
        base64_str = base64_str.split(',')[1]
    # 解码 Base64 字符串
    img_data = base64.b64decode(base64_str)
    # 保存为图片文件
    with open(file_name, 'wb') as img_file:
        img_file.write(img_data)
    logger.success(f"图片已保存为 {file_name}")


def download_ts_files(m3u8_url, category, dir_name, file_name) -> str:
    dir_name = dir_name.replace(' ', '')
    base_path = f'{settings.video_download_path}/{category}/{dir_name}'
    if not os.path.exists(base_path):
        os.makedirs(base_path)
    video_path = f'{base_path}/{file_name}'.replace(' ', '')
    if os.path.exists(video_path):
        return video_path

    ffmpeg_command = f'ffmpeg -protocol_whitelist file,http,https,tcp,tls,crypto -i "{m3u8_url}" -c copy "{video_path}"'
    logger.info(f"开始下载视频: {ffmpeg_command}")
    subprocess.run(ffmpeg_command, shell = True)
    logger.success(f"视频已保存为 {video_path}")
    return video_path


def _download_ts_files(m3u8_url: str, category: str, dir_name: str, file_name: str) -> str:
    """
    实际执行下载的函数（阻塞调用 FFmpeg）
    """
    dir_name = dir_name.replace(' ', '')
    base_path = f'{settings.video_download_path}/{category}/{dir_name}'
    os.makedirs(base_path, exist_ok = True)

    video_path = f'{base_path}/{file_name}'.replace(' ', '')
    if os.path.exists(video_path):
        logger.info(f"文件已存在，直接返回: {video_path}")
        return video_path

    ffmpeg_command = f'ffmpeg -protocol_whitelist file,http,https,tcp,tls,crypto -i "{m3u8_url}" -c copy "{video_path}"'
    logger.info(f"开始下载视频: {ffmpeg_command}")
    subprocess.run(ffmpeg_command, shell = True)
    logger.success(f"视频已保存为 {video_path}")
    return video_path


def submit_download_task(m3u8_url: str, category: str, dir_name: str, file_name: str) -> str:
    """
    提交下载任务到线程池，立即返回
    task_id 用于查询状态或关联 token
    """
    if m3u8_url in download_tasks:
        logger.warning(f"任务 {m3u8_url} 已存在")
        return "task_exists"

    future = download_executor.submit(_download_ts_files, m3u8_url, category, dir_name, file_name)
    download_tasks[m3u8_url] = future
    logger.info(f"任务 {m3u8_url} 已提交到后台下载线程")
    return f"task_submitted: {m3u8_url}"


if __name__ == '__main__':
    download_ts_files(
        'https://hls.woztrh.cn/videos5/2ef988519a4fe260655aa258c0500e65/2ef988519a4fe260655aa258c0500e65.m3u8?auth_key=1760621901-68f0f54d9ec29-0-f05c2bb52d52b8e649d0ffdb0ac51eab&v=3&time=0',
        '51'
        'A1.mp4')
