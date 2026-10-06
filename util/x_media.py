import json
import re
import unicodedata
import uuid
from urllib.parse import quote

from config.config import settings
from util.cache_util import r


def video_filename(text: str, status_id: str, index: int) -> str:
    text = re.sub(r'https?://\S+', '', unicodedata.normalize('NFKC', text or ''))
    text = ''.join(' ' if c in '\r\n\t' else c for c in text if unicodedata.category(c)[0] != 'C' or c in '\r\n\t')
    text = re.sub(r'[<>:"/\\|?*]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip(' .')
    # Leave space for the ID, sequence and extension within common filesystem limits.
    while len(text.encode('utf-8')) > 150:
        text = text[:-1]
    text = text.rstrip(' .') or '视频'
    return f'{text}_{status_id}_{index}.mp4'


def named_video(url: str, text: str, status_id: str, index: int) -> str:
    filename = video_filename(text, status_id, index)
    token = str(uuid.uuid4())
    r.set('x-media:' + token, json.dumps({'url': url, 'filename': filename}),
          ex=settings.file_expire_seconds)
    return f'{settings.network_url}/api/v1/x-media/{token}/{quote(filename, safe="")}'


def get_media(token: str):
    value = r.get('x-media:' + token)
    return json.loads(value) if value else None
