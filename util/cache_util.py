import json
import uuid

import redis

from config.config import settings

r = redis.Redis(host = settings.redis_host, port = settings.redis_port, db = settings.redis_db,
                password = settings.redis_pwd or None, decode_responses = True,
                socket_connect_timeout = 5, socket_timeout = 5)


def create_token(file_path, expire_seconds = 3600):
    """
    生成 token 并保存映射
    :param file_path: 存放路径
    :param expire_seconds: 有效期
    :return:
    """
    token = str(uuid.uuid4())
    value = json.dumps({"path": file_path})
    r.set(token, value, ex = expire_seconds)  # ex=秒数
    return token


def get_path(token):
    """
    # 获取 token 对应的文件的路径
    :param token:
    :return:
    """
    value = r.get(token)
    if not value:
        return None
    return json.loads(value)["path"]


# 删除 token（可选）
def delete_token(token):
    r.delete(token)
