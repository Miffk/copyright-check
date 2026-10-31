"""Обновление программы из релизов GitHub.

Собранный .exe обновляет себя сам: скачивает новый файл рядом с собой,
сверяет SHA-256, переименовывает себя в *.old.exe (Windows разрешает
переименовать запущенный файл, но не удалить его), кладёт новый на своё
место и запускает его. Старый файл удаляется при следующем старте.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from version import __version__

REPO = "Miffk/copyright-check"
ASSET_NAME = "CopyrightCheck.exe"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
TIMEOUT = 20
CHUNK = 256 * 1024


class UpdateCancelled(Exception):
    pass


@dataclass
class Release:
    version: str
    notes: str
    page: str
    download_url: str = ""
    size: int = 0
    sha256: str = ""


def parse_version(text: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", text))


def is_newer(version: str) -> bool:
    return parse_version(version) > parse_version(__version__)


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": f"CopyrightCheck/{__version__}"})


def latest_release() -> Release:
    url = f"https://api.github.com/repos/{REPO}/releases/latest"
    with urllib.request.urlopen(_request(url), timeout=TIMEOUT) as response:
        data = json.load(response)
    release = Release(
        version=data.get("tag_name", "").lstrip("v"),
        notes=data.get("body") or "",
        page=data.get("html_url") or RELEASES_PAGE,
    )
    for asset in data.get("assets", []):
        if asset.get("name") == ASSET_NAME:
            release.download_url = asset.get("browser_download_url", "")
            release.size = asset.get("size", 0)
            digest = asset.get("digest") or ""
            if digest.startswith("sha256:"):
                release.sha256 = digest.split(":", 1)[1]
    return release


def current_exe() -> Path | None:
    """Путь к собранному .exe или None, если запущено из исходников."""
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        return Path(sys.executable)
    return None


def can_self_update() -> bool:
    exe = current_exe()
    return exe is not None and os.access(exe.parent, os.W_OK)


def cleanup_previous() -> None:
    exe = current_exe()
    if exe is None:
        return
    for leftover in (exe.with_suffix(".old.exe"), exe.with_suffix(".download")):
        try:
            leftover.unlink(missing_ok=True)
        except OSError:
            pass  # ещё занят — удалим в следующий раз


def download(
    release: Release,
    progress: Callable[[int, int], None],
    cancelled: Callable[[], bool],
) -> Path:
    exe = current_exe()
    target = exe.with_suffix(".download")
    digest = hashlib.sha256()
    received = 0
    try:
        with urllib.request.urlopen(_request(release.download_url), timeout=TIMEOUT) as response, \
                open(target, "wb") as out:
            total = release.size or int(response.headers.get("Content-Length") or 0)
            while chunk := response.read(CHUNK):
                if cancelled():
                    raise UpdateCancelled
                out.write(chunk)
                digest.update(chunk)
                received += len(chunk)
                progress(received, total)
        if release.size and received != release.size:
            raise RuntimeError("Файл скачался не полностью, попробуйте ещё раз.")
        if release.sha256 and digest.hexdigest() != release.sha256:
            raise RuntimeError("Скачанный файл повреждён (не совпала контрольная сумма).")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def install_and_restart(new_file: Path) -> None:
    """Подменяет запущенный .exe новым и запускает его. Вызывающий должен сразу выйти."""
    exe = current_exe()
    old = exe.with_suffix(".old.exe")
    old.unlink(missing_ok=True)
    exe.rename(old)
    try:
        new_file.rename(exe)
    except OSError:
        old.rename(exe)  # вернуть как было
        raise
    # Без этого новый onefile-процесс подхватит временную папку старого и упадёт
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
    subprocess.Popen(
        [str(exe)], env=env, close_fds=True,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
