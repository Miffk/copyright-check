"""Поиск трека по названию в каталоге Deezer: правообладатель, год, ISRC.

API бесплатное и без ключа. Поиск отдаёт треки с ISRC, а лейбл (он же
правообладатель записи) лежит в карточке альбома, её запрашиваем отдельно.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

API = "https://api.deezer.com"
TIMEOUT = 15

# Лейблы, чья музыка разрешена в видео на известных условиях
FREE_LABELS = (
    ("ncs", "free", "NoCopyrightSounds (NCS) разрешает бесплатно использовать треки в видео "
     "и стримах, если указать автора и ссылку в описании. Условия: ncs.io/usage-policy"),
    ("nocopyrightsounds", "free", "NoCopyrightSounds (NCS) разрешает бесплатно использовать треки "
     "в видео и стримах, если указать автора и ссылку в описании. Условия: ncs.io/usage-policy"),
    ("streambeats", "free", "StreamBeats разрешает бесплатно использовать музыку в стримах и видео "
     "на YouTube и Twitch. Условия: streambeats.com"),
    ("epidemic sound", "license", "Музыка Epidemic Sound. В видео можно только по подписке "
     "Epidemic Sound, привязав к ней свой канал."),
    ("artlist", "license", "Музыка Artlist. В видео можно только по подписке Artlist."),
    ("monstercat", "license", "Музыка Monstercat. Для стримов и видео нужна лицензия "
     "Monstercat Gold или разрешение лейбла."),
)


@dataclass
class FoundTrack:
    title: str
    artist: str
    album: str
    label: str = ""
    released: str = ""
    isrc: str = ""
    duration: int = 0
    link: str = ""
    preview: str = ""
    cover: bytes = b""

    def usage(self) -> tuple[str, str]:
        """(уровень, пояснение): free — разрешено на условиях, license — нужна подписка,
        commercial — обычный релиз, без лицензии будет заявка."""
        for key, level, text in FREE_LABELS:
            if re.search(rf"\b{re.escape(key)}\b", self.label, re.IGNORECASE):
                return level, text
        return "commercial", (
            "Обычный релиз. Без лицензии от правообладателя YouTube, скорее всего, "
            "найдёт этот трек через Content ID и поставит заявку.")


def _get(path: str, **params) -> dict:
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": "CopyrightCheck/1.1"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        data = json.load(response)
    if isinstance(data, dict) and "error" in data:
        error = data["error"]
        raise RuntimeError(f"Deezer: {error.get('message', error)}")
    return data


def _download(url: str) -> bytes:
    if not url:
        return b""
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
            return response.read()
    except Exception:
        return b""  # без обложки обойдёмся


def search_tracks(query: str, limit: int = 10) -> list[FoundTrack]:
    items = _get("/search", q=query, limit=limit).get("data", [])
    tracks = [
        FoundTrack(
            title=item.get("title", ""),
            artist=item.get("artist", {}).get("name", ""),
            album=item.get("album", {}).get("title", ""),
            isrc=item.get("isrc", ""),
            duration=item.get("duration", 0),
            link=item.get("link", ""),
            preview=item.get("preview", ""),
        )
        for item in items
    ]
    album_ids = list(dict.fromkeys(item["album"]["id"] for item in items if item.get("album")))

    def album(album_id: int) -> dict:
        try:
            return _get(f"/album/{album_id}")
        except Exception:
            return {}

    with ThreadPoolExecutor(max_workers=5) as pool:
        albums = dict(zip(album_ids, pool.map(album, album_ids)))
        covers = list(pool.map(_download, [item.get("album", {}).get("cover_small", "") or "" for item in items]))
    for item, track, cover in zip(items, tracks, covers):
        info = albums.get(item.get("album", {}).get("id"), {})
        track.label = info.get("label", "")
        track.released = info.get("release_date", "")
        track.cover = cover
    return tracks


if __name__ == "__main__":
    import sys

    for t in search_tracks(" ".join(sys.argv[1:])):
        level, text = t.usage()
        print(f"{t.artist} — {t.title} | {t.album} | {t.label} | {t.released} | {t.isrc} | {level}")
