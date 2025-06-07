import os
import sys
import dataclasses
import platform
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from telegram.client import Telegram


chat_id = 0
api_id = 'xxx'
api_hash = 'xxx'
phone = 'xxx'



class TgException(Exception):
    ...


@dataclasses.dataclass
class TgVideo:
    caption: Optional[str]
    duration: timedelta
    expected_size: int
    downloaded_size: int
    file_id: int
    message_id: int
    album: Optional[list]
    local_path: Optional[str] = None
    source_group: Optional[str] = None

    @property
    def completed(self) -> bool:
        return self.expected_size == self.downloaded_size


class TgClient:
    def __init__(self) -> None:
        arch = platform.machine()
        path = str((Path(__file__).parent.parent / 'libtdjson.so').resolve())
        self._client = Telegram(
            api_id=api_id,
            api_hash=api_hash,
            phone=phone,
            database_encryption_key='777888999',
            files_directory='/data/data/com.termux/files/home/storage/downloads/Telegram/.tg',
            library_path=path if arch == 'aarch64' else None,
        )
        self._client.add_update_handler('updateFile', self._update_file_handler)
        self._download_callbacks = []
        self._videos = {}
        self._loading = {}

    def init(self) -> None:
        try:
            self._client.login()
        except Exception as e:
            raise TgException(f'cant login: {e}')

        self._call_wrap('get_chats')

    def _call(self, method: str, **params) -> dict:
        result = self._client.call_method(method, params)
        result.wait()
        if result.error:
            raise TgException(f'cant {method}: {result.error_info}')

        return result.update

    def _call_wrap(self, method: str, *args, **kwargs):
        result = getattr(self._client, method)(*args, **kwargs)
        result.wait()
        if result.error:
            raise TgException(f'cant wrap {method}: {result.error_info}')

        return result.update

    def add_download_callback(self, callback) -> None:
        self._download_callbacks.append(callback)

    def remove_download_callback(self, callback) -> None:
        if callback in self._download_callbacks:
            self._download_callbacks.remove(callback)

    def _update_file_handler(self, event: dict) -> None:
        event_file = event['file']
        video = self._videos.get(event_file['id'])
        if not video:
            return

        local_file = event_file['local']
        video.local_path = local_file['path'] or None
        video.downloaded_size = local_file['downloaded_size']

        if video.completed:
            self._remove_from_queue(video)

        for cb in self._download_callbacks:
            cb(video)

    def _fetch_page(self, limit: int = 100, last_message_id: int = None) -> dict[int, TgVideo]:
        result = self._call_wrap('get_chat_history', chat_id, limit=limit, from_message_id=last_message_id)
        messages = result['messages']

        videos = {}
        albums = defaultdict(list)
        for message in messages:
            album_id = message['media_album_id']
            album = None
            if album_id != '0':
                album = albums[album_id]

            video = message['content'].get('video')
            if not video:
                if album is not None:
                    # todo: set caption
                    album.append(message['id'])
                continue

            video_file = video['video']
            local_file = video_file['local']
            videos[video_file['id']] = TgVideo(
                caption=message['content']['caption'].get('text'),
                duration=video['duration'],
                expected_size=video_file['size'],
                downloaded_size=local_file['downloaded_size'],
                local_path=local_file['path'],
                file_id=video_file['id'],
                message_id=message['id'],
                album=album,
            )

        return videos

    def list_videos(self, limit: int = 100) -> list[TgVideo]:
        self._videos = self._fetch_page(limit)
        return self.videos

    def load_next(self, limit: int = 10) -> None:
        if not self.videos:
            self.list_videos()
            return

        last_message_id = self.videos[-1].message_id
        page = self._fetch_page(limit, last_message_id)
        self._videos = {**self._videos, **page}

    @property
    def videos(self) -> list[TgVideo]:
        return list(self._videos.values())

    def download_video(self, video: TgVideo) -> Optional[int]:
        for i in range(2, 33):
            if i not in self._loading:
                break
        else:
            return

        self._loading[i] = video
        self._call('downloadFile', file_id=video.file_id, priority=i)
        return i

    def _remove_from_queue(self, video):
        for k, v in self._loading.items():
            if v == video:
                self._loading.pop(k)
                break

    def cancel_download_video(self, video: TgVideo) -> None:
        self._remove_from_queue(video)
        self._call('cancelDownloadFile', file_id=video.file_id)

    def delete_video(self, video: TgVideo) -> None:
        self._call('deleteFile', file_id=video.file_id)

    def delete_message(self, video: TgVideo) -> None:
        ids = [video.message_id]
        if video.album:
            ids.extend(video.album)
        self._call('deleteMessages', chat_id=chat_id, message_ids=ids)
        self._videos.pop(video.file_id)


if __name__ == '__main__':
    tg = TgClient()
