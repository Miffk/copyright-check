"""Распознавание музыки в аудио/видео файле через базу Shazam.

Файл декодируется ffmpeg в моно 16 кГц, режется на 12-секундные фрагменты,
каждый фрагмент отправляется на распознавание. Совпадения одного и того же
трека в соседних фрагментах склеиваются в один интервал.
"""

from __future__ import annotations

import asyncio
import io
import math
import subprocess
import sys
import time
import urllib.request
import wave
from dataclasses import dataclass, field
from typing import Callable

import imageio_ffmpeg
from aiohttp_retry import ExponentialRetry
from shazamio import RateLimited, Shazam
from shazamio.client import HTTPClient

SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2  # моно, 16 бит
WINDOW_SECONDS = 12  # такое окно шлют клиенты Shazam; от 15 сек он перестаёт находить
MIN_TAIL_SECONDS = 4  # более короткий хвост файла не проверяем
# Shazam пропускает ~3 запроса подряд, дальше примерно один в 20 секунд
BURST = 3
REFILL_SECONDS = 21
MAX_RATE_LIMIT_RETRIES = 5  # сколько раз подряд терпеть отказ «слишком часто»

MODES = {
    "fast": 30,  # шаг между началами фрагментов, сек
    "thorough": 12,
}

AUDIO_VIDEO_EXTENSIONS = (
    ".mp3", ".wav", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wma", ".aiff", ".aif",
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".flv", ".m4v",
)


class CheckCancelled(Exception):
    pass


@dataclass
class TrackInfo:
    key: str
    title: str
    artist: str
    album: str = ""
    label: str = ""
    released: str = ""
    isrc: str = ""
    url: str = ""


@dataclass
class Segment:
    start: float
    end: float
    track: TrackInfo | None = None
    error: str = ""
    checked: bool = False


@dataclass
class Match:
    track: TrackInfo
    ranges: list[tuple[float, float]] = field(default_factory=list)
    segments: int = 0

    @property
    def weak(self) -> bool:
        # Одиночное попадание бывает ложным срабатыванием
        return self.segments < 2


@dataclass
class CheckResult:
    path: str
    duration: float
    segments: list[Segment]
    matches: list[Match]
    rate_limited: bool = False
    unreachable: bool = False  # Shazam не ответил ни разу — нет связи или блокировка
    proxy: str = ""  # через какой прокси шли запросы, пусто — напрямую
    eta: float = 0  # сколько примерно секунд осталось

    @property
    def errors(self) -> int:
        return sum(1 for s in self.segments if s.error)

    @property
    def checked_ok(self) -> int:
        return sum(1 for s in self.segments if s.checked and not s.error)

    @property
    def error_text(self) -> str:
        return next((s.error for s in self.segments if s.error), "")

    @property
    def done(self) -> int:
        return sum(1 for s in self.segments if s.checked)


def format_time(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def decode_audio(path: str) -> bytes:
    """Возвращает сырой PCM s16le, моно, 16 кГц."""
    cmd = [
        imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-nostdin",
        "-i", path, "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-",
    ]
    # В собранном .exe без консоли иначе на каждый вызов мигает чёрное окно
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    proc = subprocess.run(cmd, capture_output=True, creationflags=flags)
    if proc.returncode != 0 or not proc.stdout:
        message = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(
            "Не удалось прочитать звук из файла"
            + (f": {message[-1]}" if message else " (в файле нет звуковой дорожки?)")
        )
    return proc.stdout


def to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def plan_segments(duration: float, step: int) -> list[Segment]:
    segments = []
    start = 0.0
    while start < duration:
        end = min(start + WINDOW_SECONDS, duration)
        if end - start >= MIN_TAIL_SECONDS or not segments:
            segments.append(Segment(start, end))
        start += step
    return segments


def parse_track(answer: dict) -> TrackInfo | None:
    track = answer.get("track")
    if not track:
        return None
    info = TrackInfo(
        key=str(track.get("key", "")),
        title=track.get("title", "Без названия"),
        artist=track.get("subtitle", ""),
        isrc=track.get("isrc", ""),
        url=track.get("url", ""),
    )
    for section in track.get("sections", []):
        for item in section.get("metadata", []) or []:
            name, text = item.get("title", ""), item.get("text", "")
            if name == "Album":
                info.album = text
            elif name == "Label":
                info.label = text
            elif name == "Released":
                info.released = text
    return info


def group_matches(segments: list[Segment]) -> list[Match]:
    matches: dict[str, Match] = {}
    for seg in segments:
        if not seg.track:
            continue
        match = matches.setdefault(seg.track.key, Match(seg.track))
        match.segments += 1
        if match.ranges and seg.start <= match.ranges[-1][1]:
            match.ranges[-1] = (match.ranges[-1][0], seg.end)
        else:
            match.ranges.append((seg.start, seg.end))
    return sorted(matches.values(), key=lambda m: m.ranges[0][0])


def system_proxy() -> str:
    """HTTP-прокси из настроек Windows (или переменных окружения).

    Многие VPN-клиенты (v2rayN, Hiddify, Clash, Happ) по умолчанию не заворачивают
    весь трафик, а включают системный прокси. urllib его видит сам, aiohttp — нет,
    и без этого запросы к Shazam шли бы в обход VPN.
    """
    proxies = urllib.request.getproxies()
    for key in ("https", "http"):
        url = proxies.get(key, "")
        if url.startswith(("http://", "https://")):
            # Локальные прокси VPN-клиентов принимают обычный HTTP
            return "http://" + url.split("://", 1)[1].rstrip("/")
    return ""


class RateLimiter:
    """Ведро токенов под лимит Shazam. Одно на весь сеанс, чтобы следующий
    файл в очереди не начинал с отказа."""

    def __init__(self) -> None:
        self.tokens = float(BURST)
        self.stamp = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(BURST, self.tokens + (now - self.stamp) / REFILL_SECONDS)
        self.stamp = now

    def wait_time(self) -> float:
        self._refill()
        return max(0.0, (1 - self.tokens) * REFILL_SECONDS)

    def take(self) -> None:
        self._refill()
        self.tokens -= 1

    def penalize(self, strikes: int) -> None:
        # Отказ значит, что сервер считает иначе, чем мы: ждём дольше с каждым разом
        self._refill()
        self.tokens = -strikes

    def eta(self, requests: int) -> float:
        self._refill()
        return max(0.0, requests - self.tokens) * REFILL_SECONDS


limiter = RateLimiter()


async def _recognize_all(
    pcm: bytes,
    result: CheckResult,
    progress: Callable[[CheckResult, int], None],
    cancelled: Callable[[], bool],
) -> None:
    # Повторы по 429 делаем сами, чтобы показывать паузу и не долбить сервер
    client = HTTPClient(retry_options=ExponentialRetry(attempts=3, statuses={500, 502, 503, 504}))
    # Названия полей метаданных (Album, Label...) parse_track ждёт на английском
    shazam = Shazam(language="en-US", endpoint_country="GB", http_client=client)

    def report(wait: int = 0) -> None:
        result.eta = limiter.eta(len(result.segments) - result.done)
        progress(result, wait)

    async def pause(seconds: float) -> None:
        for left in range(math.ceil(seconds), 0, -1):
            if cancelled():
                raise CheckCancelled
            report(left)
            await asyncio.sleep(1)

    try:
        for i, seg in enumerate(result.segments):
            if cancelled():
                raise CheckCancelled
            chunk = pcm[int(seg.start * SAMPLE_RATE) * 2:int(seg.end * SAMPLE_RATE) * 2]
            strikes = 0
            while True:
                await pause(limiter.wait_time())
                limiter.take()
                try:
                    answer = await shazam.recognize(to_wav(chunk), proxy=result.proxy or None)
                    seg.track = parse_track(answer)
                    seg.error = ""
                    break
                except RateLimited:
                    strikes += 1
                    if strikes > MAX_RATE_LIMIT_RETRIES:
                        result.rate_limited = True
                        for rest in result.segments[i:]:
                            rest.error, rest.checked = "Shazam ограничил запросы", True
                        report()
                        return
                    limiter.penalize(strikes)
                except Exception as exc:  # сеть и прочее: фрагмент пропускаем
                    seg.error = str(exc) or exc.__class__.__name__
                    break
            seg.checked = True
            if seg.error and i >= 1 and result.checked_ok == 0:
                # Два отказа подряд с самого начала: Shazam недоступен, дальше нет смысла
                result.unreachable = True
                for rest in result.segments[i + 1:]:
                    rest.error, rest.checked = seg.error, True
                report()
                return
            result.matches = group_matches(result.segments)
            report()
    finally:
        await client.close()


def check_file(
    path: str,
    mode: str = "thorough",
    progress: Callable[[CheckResult, int], None] = lambda result, wait: None,
    cancelled: Callable[[], bool] = lambda: False,
) -> CheckResult:
    """progress(result, wait) вызывается после каждого фрагмента и раз в секунду
    во время паузы, которую потребовал Shazam (wait — сколько секунд осталось)."""
    pcm = decode_audio(path)
    duration = len(pcm) / BYTES_PER_SECOND
    result = CheckResult(path, duration, plan_segments(duration, MODES[mode]), [])
    result.proxy = system_proxy()
    result.eta = limiter.eta(len(result.segments))
    progress(result, 0)
    asyncio.run(_recognize_all(pcm, result, progress, cancelled))
    return result


def report_text(result: CheckResult) -> str:
    lines = [
        f"Файл: {result.path}",
        f"Длительность: {format_time(result.duration)}",
        f"Фрагментов: {len(result.segments)}",
        "",
    ]
    if result.checked_ok == 0:
        lines.append("ПРОВЕРКА НЕ ВЫПОЛНЕНА: не удалось связаться с Shazam.")
        lines.append(f"Ошибка: {result.error_text}")
        return "\n".join(lines)
    if result.matches:
        lines.append(f"НАЙДЕНЫ СОВПАДЕНИЯ ({len(result.matches)}):")
        for m in result.matches:
            lines.append("")
            lines.append(f"  {m.track.artist} — {m.track.title}" + ("  [слабое совпадение]" if m.weak else ""))
            for k, v in (("Альбом", m.track.album), ("Лейбл", m.track.label),
                         ("Год", m.track.released), ("ISRC", m.track.isrc), ("Ссылка", m.track.url)):
                if v:
                    lines.append(f"    {k}: {v}")
            spans = ", ".join(f"{format_time(a)}–{format_time(b)}" for a, b in m.ranges)
            lines.append(f"    Звучит в файле: {spans}")
    else:
        lines.append("Совпадений с известными записями не найдено.")
    if result.errors:
        reason = "Shazam временно ограничил число запросов" if result.rate_limited else "ошибка сети?"
        lines += ["", f"Не удалось проверить фрагментов: {result.errors} ({reason})"]
    lines += [
        "",
        "Проверка выполнена по базе Shazam. Отсутствие совпадений не гарантирует,",
        "что в файле нет чужой музыки: её может просто не быть в базе.",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    # Проверка из командной строки: python checker.py файл [fast|thorough]
    target = sys.argv[1]
    res = check_file(target, sys.argv[2] if len(sys.argv) > 2 else "thorough",
                     progress=lambda r, wait: print(f"\r{r.done}/{len(r.segments)}"
                                                    + (f" пауза {wait} сек " if wait else "        "),
                                                    end="", flush=True))
    print()
    print(report_text(res))
