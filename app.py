"""Окно программы «Проверка авторских прав»."""

from __future__ import annotations

import html
import math
import os
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QRectF, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QProgressDialog, QPushButton, QSplitter,
    QTabWidget, QTableWidget, QTableWidgetItem, QTextBrowser, QToolTip, QVBoxLayout, QWidget,
)

import checker
import search
import updater
from version import __version__

APP_TITLE = "Проверка авторских прав"

COLOR_CLEAN = QColor("#3fa66b")
COLOR_MATCH = QColor("#d64545")
COLOR_WEAK = QColor("#e39b2d")
COLOR_ERROR = QColor("#8a8f98")


def media_files(paths: list[str]) -> list[str]:
    """Раскрывает папки и отбрасывает файлы, не похожие на аудио/видео."""
    found = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            found += sorted(str(f) for f in path.rglob("*")
                            if f.suffix.lower() in checker.AUDIO_VIDEO_EXTENSIONS)
        elif path.is_file():
            found.append(str(path))
    return found


class Worker(QObject):
    progress = Signal(int, object, int)  # строка, частичный результат, секунд паузы
    finished = Signal(int, object)
    failed = Signal(int, str)

    def __init__(self, row: int, path: str, mode: str):
        super().__init__()
        self.row, self.path, self.mode = row, path, mode
        self.stop_requested = False

    def run(self) -> None:
        try:
            result = checker.check_file(
                self.path, self.mode,
                progress=lambda result, wait: self.progress.emit(self.row, result, wait),
                cancelled=lambda: self.stop_requested,
            )
        except checker.CheckCancelled:
            self.failed.emit(self.row, "Остановлено")
        except Exception as exc:
            self.failed.emit(self.row, str(exc))
        else:
            self.finished.emit(self.row, result)


class DropZone(QFrame):
    files_dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(110)
        self.setObjectName("dropZone")
        layout = QVBoxLayout(self)
        title = QLabel("Перетащите сюда музыку или видео")
        font = title.font()
        font.setPointSize(font.pointSize() + 4)
        font.setBold(True)
        title.setFont(font)
        hint = QLabel("или нажмите, чтобы выбрать файлы · можно сразу несколько или целую папку")
        for label in (title, hint):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(label)
        self._set_active(False)

    def _set_active(self, active: bool) -> None:
        self.setProperty("active", active)
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event):
        exts = " ".join(f"*{e}" for e in checker.AUDIO_VIDEO_EXTENSIONS)
        files, _ = QFileDialog.getOpenFileNames(
            self, "Выберите файлы", "", f"Аудио и видео ({exts});;Все файлы (*)")
        if files:
            self.files_dropped.emit(files)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._set_active(True)

    def dragLeaveEvent(self, event):
        self._set_active(False)

    def dropEvent(self, event):
        self._set_active(False)
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.files_dropped.emit(paths)


class Timeline(QWidget):
    """Полоса по длительности файла: где нашлась чужая музыка."""

    def __init__(self):
        super().__init__()
        self.result: checker.CheckResult | None = None
        self.weak_keys: set[str] = set()
        self.setMinimumHeight(52)
        self.setMouseTracking(True)

    def set_result(self, result: checker.CheckResult | None) -> None:
        self.result = result
        self.weak_keys = {m.track.key for m in result.matches if m.weak} if result else set()
        self.setVisible(result is not None)
        self.update()

    def _bar(self) -> QRectF:
        return QRectF(0, 4, self.width() - 1, 26)

    def _color(self, seg: checker.Segment) -> QColor:
        if seg.track:
            return COLOR_WEAK if seg.track.key in self.weak_keys else COLOR_MATCH
        return COLOR_ERROR if seg.error else COLOR_CLEAN

    def paintEvent(self, event):
        if not self.result or self.result.duration <= 0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bar, total = self._bar(), self.result.duration
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self.palette().mid())
        p.drawRoundedRect(bar, 5, 5)
        for seg in self.result.segments:
            if not seg.checked:
                continue
            x1 = bar.left() + bar.width() * seg.start / total
            x2 = bar.left() + bar.width() * seg.end / total
            p.setBrush(self._color(seg))
            p.drawRect(QRectF(x1, bar.top(), max(x2 - x1, 1), bar.height()))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(self.palette().window().color(), 2))
        p.drawRoundedRect(bar, 5, 5)
        p.setPen(self.palette().text().color())
        y = int(bar.bottom()) + 16
        p.drawText(0, y, "0:00")
        end = checker.format_time(total)
        p.drawText(self.width() - p.fontMetrics().horizontalAdvance(end) - 2, y, end)

    def mouseMoveEvent(self, event):
        if not self.result:
            return
        bar = self._bar()
        t = (event.position().x() - bar.left()) / bar.width() * self.result.duration
        for seg in self.result.segments:
            if seg.start <= t < seg.end:
                span = f"{checker.format_time(seg.start)}–{checker.format_time(seg.end)}"
                if not seg.checked:
                    text = f"{span}: ещё не проверено"
                elif seg.track:
                    text = f"{span}: {seg.track.artist} — {seg.track.title}"
                elif seg.error:
                    text = f"{span}: не удалось проверить"
                else:
                    text = f"{span}: совпадений нет"
                QToolTip.showText(event.globalPosition().toPoint(), text, self)
                return
        QToolTip.hideText()


VPN_HINT = "В России Shazam может быть недоступен без VPN — включите VPN и проверьте файл ещё раз."
VPN_MODE_HINT = (
    "Если VPN уже включён, включите в VPN-клиенте режим, при котором через VPN идёт весь трафик: "
    "он называется TUN, «Весь трафик» или «Системный VPN». Либо включите в клиенте "
    "«Системный прокси» (System proxy) с HTTP-портом — программа подхватит его сама.")


def eta_text(seconds: float) -> str:
    minutes = round(seconds / 60)
    return f"{minutes} мин" if minutes >= 1 else f"{math.ceil(seconds)} сек"


def result_html(result: checker.CheckResult, wait: int | None = None) -> str:
    """wait не None — проверка ещё идёт (wait > 0 — Shazam попросил паузу)."""
    e = html.escape
    parts = []
    if wait is not None:
        parts.append(f"<h2>Идёт проверка… {result.done} из {len(result.segments)}</h2>")
        if result.eta >= 1:
            parts.append(f"<p>Осталось примерно {eta_text(result.eta)}.</p>")
        if wait:
            parts.append(
                f"<p style='color:gray'>Shazam принимает около трёх запросов в минуту, поэтому "
                f"между фрагментами пауза: следующий через {wait} сек. "
                "Можно свернуть окно и заняться своими делами.</p>")
        if result.matches:
            parts.append("<p><b>Уже найдено:</b></p>")
    elif result.matches:
        strong = [m for m in result.matches if not m.weak]
        color = COLOR_MATCH.name() if strong else COLOR_WEAK.name()
        head = "Найдена чужая музыка" if strong else "Возможное совпадение"
        parts.append(f"<h2 style='color:{color}'>⚠ {head}</h2>")
    if wait is None and result.matches:
        parts.append(
            "<p>Эти записи есть в базе распознавания. Скорее всего, YouTube Content ID "
            "и другие площадки тоже их обнаружат: на видео могут поставить заявку "
            "правообладателя, отключить монетизацию или заблокировать его.</p>")
    for m in result.matches:
        t = m.track
        weak = (f" <span style='color:{COLOR_WEAK.name()}'>(найдено в одном фрагменте — "
                "возможно ложное срабатывание, послушайте этот участок)</span>") if m.weak else ""
        parts.append(f"<h3>{e(t.artist)} — {e(t.title)}{weak}</h3><table cellpadding='2'>")
        spans = ", ".join(f"{checker.format_time(a)}–{checker.format_time(b)}" for a, b in m.ranges)
        rows = [("Звучит в файле", spans), ("Альбом", t.album), ("Лейбл", t.label),
                ("Год", t.released), ("ISRC", t.isrc)]
        for name, value in rows:
            if value:
                parts.append(f"<tr><td style='color:gray'>{name}</td><td><b>{e(value)}</b></td></tr>")
        if t.url:
            parts.append(f"<tr><td></td><td><a href='{e(t.url)}'>Открыть в Shazam</a></td></tr>")
        parts.append("</table>")
    if wait is not None:
        return "".join(parts)
    total = len(result.segments)
    route = f"через прокси {e(result.proxy)}" if result.proxy else "напрямую, системный прокси не найден"
    error = (f"<p style='color:gray'>Подключение: {route}.<br>Текст ошибки: {e(result.error_text)}</p>"
             if result.error_text else "")
    if result.checked_ok == 0 and result.internal_error:
        return (
            f"<h2 style='color:{COLOR_WEAK.name()}'>Проверка не выполнена: ошибка в программе</h2>"
            "<p>Это не проблема с интернетом или VPN, а сбой в самой программе. Результата нет: "
            "это <b>не</b> значит, что чужой музыки в файле нет.</p>"
            "<p>Нажмите «Проверить обновления» внизу окна: возможно, ошибка уже исправлена. "
            "Если нет — отправьте разработчику текст ошибки ниже.</p>"
            f"{error}")
    if result.checked_ok == 0 and not result.rate_limited:
        return (
            f"<h2 style='color:{COLOR_WEAK.name()}'>Проверка не выполнена</h2>"
            "<p>Не удалось связаться с сервером Shazam, поэтому ни один фрагмент не проверен. "
            "Результата нет: это <b>не</b> значит, что чужой музыки в файле нет.</p>"
            f"<p><b>{VPN_HINT}</b></p><p>{VPN_MODE_HINT}</p>{error}")
    if not result.matches:
        if result.errors:
            parts.append(f"<h2 style='color:{COLOR_WEAK.name()}'>Совпадений не найдено "
                         f"в {result.checked_ok} из {total} фрагментов</h2>")
        else:
            parts.append(f"<h2 style='color:{COLOR_CLEAN.name()}'>✓ Совпадений не найдено</h2>")
        parts.append(
            "<p>Ни один проверенный фрагмент не совпал с известными записями. Это хороший знак, "
            "но не гарантия: малоизвестных треков, свежих релизов и библиотек "
            "продакшн-музыки может не быть в базе.</p>")
    if result.rate_limited:
        parts.append(
            f"<p style='color:{COLOR_WEAK.name()}'>Shazam временно ограничил число запросов, "
            f"поэтому {result.errors} из {len(result.segments)} фрагментов не проверены "
            "(серые участки на полосе). Подождите 10–15 минут и проверьте файл ещё раз.</p>")
    elif result.errors:
        parts.append(
            f"<p style='color:{COLOR_WEAK.name()}'>Не удалось проверить фрагментов: {result.errors} "
            f"из {total} (серые участки на полосе). Проверьте интернет и запустите проверку "
            f"ещё раз. {VPN_HINT}</p>{error}")
    parts.append(
        f"<p style='color:gray'>Длительность {checker.format_time(result.duration)}, "
        f"проверено фрагментов: {len(result.segments)}.</p>")
    return "".join(parts)


SEARCH_EXAMPLES = ("Imagine Dragons Believer", "Кино Группа крови", "Elektronomia Sky High")
USAGE_COLORS = {"free": COLOR_CLEAN, "license": COLOR_WEAK, "commercial": COLOR_MATCH}
USAGE_TITLES = {"free": "Можно использовать на условиях лейбла", "license": "Нужна подписка",
                "commercial": "Нужна лицензия"}


def search_help_html() -> str:
    examples = " · ".join(f"<a href='example:{i}'>{html.escape(q)}</a>"
                          for i, q in enumerate(SEARCH_EXAMPLES))
    return (
        "<h3>Поиск по названию</h3>"
        "<p>Введите исполнителя и название песни, и программа найдёт трек в каталоге Deezer "
        "(больше 100 миллионов записей). Она покажет, кто правообладатель записи, год выпуска, "
        "ISRC, а также можно ли взять трек в видео.</p>"
        "<p>Удобно, чтобы проверить трек ещё до монтажа. Если файл уже есть, надёжнее "
        "проверить сам файл на первой вкладке.</p>"
        f"<p>Например: {examples}</p>")


def search_results_html(query: str, tracks: list[search.FoundTrack], request: int) -> str:
    e = html.escape
    if not tracks:
        return (f"<h3>По запросу «{e(query)}» ничего не найдено</h3>"
                "<p>Попробуйте написать иначе: исполнитель и название так, как они написаны "
                "на обложке, без лишних слов вроде «скачать» или «песня».</p>")
    parts = [f"<p style='color:gray'>Найдено по запросу «{e(query)}»: {len(tracks)}</p>"]
    for i, t in enumerate(tracks):
        level, note = t.usage()
        color = USAGE_COLORS[level].name()
        cover = (f"<img src='cover:{request}-{i}' width='56' height='56'>" if t.cover else "")
        meta = " · ".join(x for x in (t.album, t.released[:4], checker.format_time(t.duration) if t.duration else "") if x)
        links = [f"<a href='{e(t.link)}'>Открыть в Deezer</a>"] if t.link else []
        if t.preview:
            links.append(f"<a href='{e(t.preview)}'>Послушать 30 сек</a>")
        parts.append(
            "<table cellpadding='4' cellspacing='0' style='margin-top:10px'><tr>"
            f"<td valign='top' width='64'>{cover}</td><td valign='top'>"
            f"<b style='font-size:large'>{e(t.artist)} — {e(t.title)}</b><br>"
            f"<span style='color:gray'>{e(meta)}</span><br>"
            + (f"Правообладатель записи: <b>{e(t.label)}</b><br>" if t.label else "")
            + (f"ISRC: {e(t.isrc)}<br>" if t.isrc else "")
            + f"<span style='color:{color}'><b>{USAGE_TITLES[level]}.</b> {e(note)}</span><br>"
            + " &nbsp; ".join(links)
            + "</td></tr></table>")
    parts.append(
        "<p style='color:gray'>Данные каталога Deezer. Кроме правообладателя записи, права на саму "
        "песню (музыку и текст) есть у авторов и издателей, поэтому кавер или своё исполнение "
        "чужой песни тоже требует разрешения.</p>")
    return "".join(parts)


class CoverBrowser(QTextBrowser):
    """QTextBrowser, который берёт обложки из памяти, а не из сети."""

    def __init__(self):
        super().__init__()
        self.images: dict[str, QImage] = {}

    def loadResource(self, kind, url):
        if url.scheme() == "cover":
            return self.images.get(url.toString())
        return super().loadResource(kind, url)


class SearchSignals(QObject):
    done = Signal(int, str, object)
    failed = Signal(int, str)


class SearchTab(QWidget):
    def __init__(self):
        super().__init__()
        self.request = 0
        self.signals = SearchSignals()
        self.signals.done.connect(self.on_done)
        self.signals.failed.connect(self.on_failed)

        self.query = QLineEdit()
        self.query.setPlaceholderText("Исполнитель и название, например: Imagine Dragons Believer")
        self.query.setClearButtonEnabled(True)
        self.query.returnPressed.connect(self.run)
        font = self.query.font()
        font.setPointSize(font.pointSize() + 2)
        self.query.setFont(font)
        self.query.setMinimumHeight(36)
        self.button = QPushButton("Найти")
        self.button.setMinimumHeight(36)
        self.button.clicked.connect(self.run)
        row = QHBoxLayout()
        row.addWidget(self.query, 1)
        row.addWidget(self.button)

        self.results = CoverBrowser()
        self.results.setOpenLinks(False)
        self.results.anchorClicked.connect(self.open_link)
        self.results.setHtml(search_help_html())

        layout = QVBoxLayout(self)
        layout.addLayout(row)
        layout.addWidget(self.results, 1)

    def open_link(self, url: QUrl) -> None:
        if url.scheme() == "example":
            self.query.setText(SEARCH_EXAMPLES[int(url.path())])
            self.run()
        else:
            QDesktopServices.openUrl(url)

    def run(self) -> None:
        text = self.query.text().strip()
        if not text:
            self.query.setFocus()
            return
        self.request += 1
        self.button.setEnabled(False)
        self.results.setHtml(f"<h3>Ищу «{html.escape(text)}»…</h3>")
        threading.Thread(target=self._work, args=(self.request, text), daemon=True).start()

    def _work(self, request: int, text: str) -> None:
        try:
            self.signals.done.emit(request, text, search.search_tracks(text))
        except Exception as exc:
            self.signals.failed.emit(request, str(exc) or exc.__class__.__name__)

    def on_done(self, request: int, text: str, tracks: list[search.FoundTrack]) -> None:
        if request != self.request:  # пришёл ответ на старый запрос
            return
        self.button.setEnabled(True)
        self.results.images = {
            f"cover:{request}-{i}": QImage.fromData(t.cover) for i, t in enumerate(tracks) if t.cover}
        self.results.setHtml(search_results_html(text, tracks, request))

    def on_failed(self, request: int, message: str) -> None:
        if request != self.request:
            return
        self.button.setEnabled(True)
        self.results.setHtml(
            "<h3>Не удалось выполнить поиск</h3>"
            f"<p>{html.escape(message)}</p><p>Проверьте интернет и попробуйте ещё раз. "
            "Каталог Deezer в России может быть недоступен без VPN.</p>")


class UpdateSignals(QObject):
    checked = Signal(object, bool)  # релиз, проверка по кнопке
    check_failed = Signal(str, bool)
    progress = Signal(int, int)
    downloaded = Signal(object)
    download_failed = Signal(str)  # пустая строка — отменено


class MainWindow(QMainWindow):
    COL_FILE, COL_DURATION, COL_STATUS = range(3)

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(980, 720)
        self.jobs: list[dict] = []  # по строкам таблицы: path, status, result, error
        self.thread: QThread | None = None
        self.worker: Worker | None = None

        self.drop = DropZone()
        self.drop.files_dropped.connect(self.add_files)

        self.mode = QComboBox()
        self.mode.addItem("Тщательная — слушать весь файл", "thorough")
        self.mode.addItem("Быстрая — 12 сек из каждых 30", "fast")
        self.stop_btn = QPushButton("Остановить")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)
        self.clear_btn = QPushButton("Очистить список")
        self.clear_btn.clicked.connect(self.clear)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Режим:"))
        controls.addWidget(self.mode)
        controls.addStretch()
        controls.addWidget(self.stop_btn)
        controls.addWidget(self.clear_btn)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Файл", "Длительность", "Результат"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(self.COL_FILE, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.COL_DURATION, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_STATUS, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(self.COL_STATUS, 260)
        self.table.itemSelectionChanged.connect(self.show_details)

        self.timeline = Timeline()
        self.timeline.setVisible(False)
        self.details = QTextBrowser()
        self.details.setOpenLinks(False)
        self.details.anchorClicked.connect(QDesktopServices.openUrl)
        self.save_btn = QPushButton("Сохранить отчёт…")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self.save_report)
        legend = QLabel(
            f"<span style='color:{COLOR_MATCH.name()}'>■</span> чужая музыка &nbsp; "
            f"<span style='color:{COLOR_WEAK.name()}'>■</span> слабое совпадение &nbsp; "
            f"<span style='color:{COLOR_CLEAN.name()}'>■</span> чисто &nbsp; "
            f"<span style='color:{COLOR_ERROR.name()}'>■</span> не проверено")
        bottom = QHBoxLayout()
        bottom.addWidget(legend)
        bottom.addStretch()
        bottom.addWidget(self.save_btn)

        detail_box = QWidget()
        detail_layout = QVBoxLayout(detail_box)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.addWidget(self.timeline)
        detail_layout.addWidget(self.details, 1)
        detail_layout.addLayout(bottom)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.table)
        splitter.addWidget(detail_box)
        splitter.setSizes([220, 420])

        disclaimer = QLabel(
            "Программа сравнивает звук с базой Shazam (десятки миллионов записей). "
            "Она показывает, чью музыку узнают автоматические системы, но не даёт юридического "
            "заключения: если у вас есть лицензия на трек, совпадение — не нарушение.")
        disclaimer.setWordWrap(True)
        disclaimer.setStyleSheet("color: gray;")

        file_tab = QWidget()
        layout = QVBoxLayout(file_tab)
        layout.addWidget(self.drop)
        layout.addLayout(controls)
        layout.addWidget(splitter, 1)
        layout.addWidget(disclaimer)

        self.search_tab = SearchTab()
        self.tabs = QTabWidget()
        self.tabs.addTab(file_tab, "Проверка файла")
        self.tabs.addTab(self.search_tab, "Поиск по названию")

        self.setup_updates()
        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.addWidget(self.update_banner)
        central_layout.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self.show_placeholder()

    # --- обновления ---

    def setup_updates(self) -> None:
        self.available: updater.Release | None = None
        self.update_cancel = False
        self.update_dialog: QProgressDialog | None = None
        self.update_signals = UpdateSignals()
        self.update_signals.checked.connect(self.on_update_checked)
        self.update_signals.check_failed.connect(self.on_update_check_failed)
        self.update_signals.progress.connect(self.on_update_progress)
        self.update_signals.downloaded.connect(self.on_update_downloaded)
        self.update_signals.download_failed.connect(self.on_update_failed)

        self.update_banner = QFrame()
        self.update_banner.setObjectName("updateBanner")
        self.update_label = QLabel()
        self.update_label.setOpenExternalLinks(True)
        self.update_btn = QPushButton("Обновить")
        self.update_btn.clicked.connect(self.start_update)
        later = QPushButton("Позже")
        later.clicked.connect(self.update_banner.hide)
        banner = QHBoxLayout(self.update_banner)
        banner.addWidget(self.update_label, 1)
        banner.addWidget(self.update_btn)
        banner.addWidget(later)
        self.update_banner.hide()

        self.check_btn = QPushButton("Проверить обновления")
        self.check_btn.setFlat(True)
        self.check_btn.clicked.connect(lambda: self.check_updates(manual=True))
        self.statusBar().addWidget(QLabel(f"Версия {__version__}"))
        self.statusBar().addPermanentWidget(self.check_btn)
        QTimer.singleShot(1500, lambda: self.check_updates(manual=False))

    def check_updates(self, manual: bool) -> None:
        self.check_btn.setEnabled(False)
        self.check_btn.setText("Проверяю…")
        threading.Thread(target=self._check_work, args=(manual,), daemon=True).start()

    def _check_work(self, manual: bool) -> None:
        try:
            self.update_signals.checked.emit(updater.latest_release(), manual)
        except Exception as exc:
            self.update_signals.check_failed.emit(str(exc) or exc.__class__.__name__, manual)

    def _reset_check_button(self) -> None:
        self.check_btn.setEnabled(True)
        self.check_btn.setText("Проверить обновления")

    def on_update_checked(self, release: updater.Release, manual: bool) -> None:
        self._reset_check_button()
        if not updater.is_newer(release.version):
            if manual:
                QMessageBox.information(self, APP_TITLE, f"У вас последняя версия ({__version__}).")
            return
        self.available = release
        self.update_label.setText(
            f"<b>Вышла новая версия {html.escape(release.version)}</b> (у вас {__version__}). "
            f"<a href='{html.escape(release.page)}'>Что нового</a>")
        self_update = updater.can_self_update() and bool(release.download_url)
        self.update_btn.setText("Обновить" if self_update else "Скачать")
        self.update_banner.show()

    def on_update_check_failed(self, message: str, manual: bool) -> None:
        self._reset_check_button()
        if manual:
            QMessageBox.warning(self, APP_TITLE, f"Не удалось проверить обновления.\n\n{message}")

    def start_update(self) -> None:
        release = self.available
        if release is None:
            return
        if not (updater.can_self_update() and release.download_url):
            # Запуск из исходников или папка без прав на запись: качаем вручную
            QDesktopServices.openUrl(QUrl(release.page))
            return
        if self.thread is not None:
            QMessageBox.information(
                self, APP_TITLE, "Сейчас идёт проверка файлов. Дождитесь её окончания "
                "или нажмите «Остановить», а потом обновитесь.")
            return
        self.update_cancel = False
        dialog = QProgressDialog("Скачиваю новую версию…", "Отмена", 0, 100, self)
        dialog.setWindowTitle(APP_TITLE)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.canceled.connect(lambda: setattr(self, "update_cancel", True))
        dialog.show()
        self.update_dialog = dialog
        threading.Thread(target=self._download_work, args=(release,), daemon=True).start()

    def _download_work(self, release: updater.Release) -> None:
        try:
            path = updater.download(
                release,
                progress=lambda done, total: self.update_signals.progress.emit(done, total),
                cancelled=lambda: self.update_cancel,
            )
        except updater.UpdateCancelled:
            self.update_signals.download_failed.emit("")
        except Exception as exc:
            self.update_signals.download_failed.emit(str(exc) or exc.__class__.__name__)
        else:
            self.update_signals.downloaded.emit(path)

    def on_update_progress(self, done: int, total: int) -> None:
        if self.update_dialog and total:
            self.update_dialog.setValue(int(done * 100 / total))
            self.update_dialog.setLabelText(
                f"Скачиваю новую версию… {done // 1048576} из {total // 1048576} МБ")

    def _close_update_dialog(self) -> None:
        if self.update_dialog:
            self.update_dialog.canceled.disconnect()
            self.update_dialog.close()
            self.update_dialog = None

    def on_update_downloaded(self, path) -> None:
        self._close_update_dialog()
        try:
            updater.install_and_restart(path)
        except Exception as exc:
            QMessageBox.warning(
                self, APP_TITLE, f"Не удалось заменить файл программы:\n{exc}\n\n"
                "Откроется страница загрузки — скачайте новую версию вручную.")
            QDesktopServices.openUrl(QUrl(self.available.page))
            return
        QApplication.quit()

    def on_update_failed(self, message: str) -> None:
        self._close_update_dialog()
        if message:
            QMessageBox.warning(self, APP_TITLE, f"Не удалось скачать обновление.\n\n{message}")

    # --- очередь проверок ---

    def add_files(self, paths: list[str]) -> None:
        files = media_files(paths)
        if not files:
            QMessageBox.information(self, APP_TITLE, "Среди выбранного нет аудио- или видеофайлов.")
            return
        for path in files:
            row = self.table.rowCount()
            self.table.insertRow(row)
            item = QTableWidgetItem(os.path.basename(path))
            item.setToolTip(path)
            self.table.setItem(row, self.COL_FILE, item)
            self.table.setItem(row, self.COL_DURATION, QTableWidgetItem(""))
            self.table.setItem(row, self.COL_STATUS, QTableWidgetItem("В очереди"))
            self.jobs.append({"path": path, "status": "queued", "result": None, "error": ""})
        if not self.table.selectedItems():
            self.table.selectRow(self.table.rowCount() - len(files))
        self.start_next()

    def start_next(self) -> None:
        if self.thread is not None:
            return
        row = next((i for i, j in enumerate(self.jobs) if j["status"] == "queued"), None)
        if row is None:
            self.stop_btn.setEnabled(False)
            return
        self.jobs[row]["status"] = "running"
        self.set_status(row, "Чтение файла…")
        self.thread = QThread()
        self.worker = Worker(row, self.jobs[row]["path"], self.mode.currentData())
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.on_progress)
        self.worker.finished.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.stop_btn.setEnabled(True)
        self.thread.start()
        self.refresh_selected(row)

    def finish_thread(self) -> None:
        self.thread.quit()
        self.thread.wait()
        self.worker.deleteLater()
        self.thread.deleteLater()
        self.thread = self.worker = None
        self.start_next()

    def on_progress(self, row: int, result: checker.CheckResult, wait: int) -> None:
        job = self.jobs[row]
        job.update(partial=result, wait=wait)
        if job["status"] != "running":  # уже остановлено
            return
        self.table.item(row, self.COL_DURATION).setText(checker.format_time(result.duration))
        status = f"Проверка… {result.done} из {len(result.segments)}"
        if result.eta >= 1:
            status += f" · осталось ≈{eta_text(result.eta)}"
        found = len(result.matches)
        self.set_status(row, status + (f" · найдено {found}" if found else ""))
        self.refresh_selected(row)

    def on_finished(self, row: int, result: checker.CheckResult) -> None:
        job = self.jobs[row]
        job.update(status="done", result=result)
        self.table.item(row, self.COL_DURATION).setText(checker.format_time(result.duration))
        if result.matches:
            strong = sum(1 for m in result.matches if not m.weak)
            text = f"⚠ Найдено треков: {len(result.matches)}"
            color = COLOR_MATCH if strong else COLOR_WEAK
        elif result.checked_ok == 0 and result.internal_error:
            text, color = "Ошибка программы", COLOR_WEAK
        elif result.checked_ok == 0 and not result.rate_limited:
            text, color = "Нет связи с Shazam", COLOR_WEAK
        elif result.errors:
            text, color = f"Не проверено фрагментов: {result.errors}", COLOR_WEAK
        else:
            text, color = "✓ Совпадений нет", COLOR_CLEAN
        self.set_status(row, text, color)
        self.refresh_selected(row)
        self.finish_thread()

    def on_failed(self, row: int, message: str) -> None:
        self.jobs[row].update(status="failed", error=message)
        self.set_status(row, "Ошибка" if message != "Остановлено" else message, COLOR_ERROR)
        self.refresh_selected(row)
        self.finish_thread()

    def stop(self) -> None:
        for job in self.jobs:
            if job["status"] == "queued":
                job.update(status="failed", error="Остановлено")
        for row, job in enumerate(self.jobs):
            if job["error"] == "Остановлено" and job["status"] == "failed":
                self.set_status(row, "Остановлено", COLOR_ERROR)
        if self.worker:
            self.worker.stop_requested = True
            self.jobs[self.worker.row]["status"] = "stopping"
            self.set_status(self.worker.row, "Остановка…")
        self.stop_btn.setEnabled(False)

    def clear(self) -> None:
        if self.thread is not None:
            QMessageBox.information(self, APP_TITLE, "Дождитесь окончания проверки или нажмите «Остановить».")
            return
        self.jobs.clear()
        self.table.setRowCount(0)
        self.show_placeholder()

    # --- отображение ---

    def set_status(self, row: int, text: str, color: QColor | None = None) -> None:
        item = self.table.item(row, self.COL_STATUS)
        item.setText(text)
        if color is not None:
            item.setForeground(color)
            font = QFont(item.font())
            font.setBold(True)
            item.setFont(font)

    def selected_row(self) -> int | None:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else None

    def refresh_selected(self, row: int) -> None:
        if self.selected_row() == row:
            self.show_details()

    def show_placeholder(self) -> None:
        self.timeline.set_result(None)
        self.save_btn.setEnabled(False)
        self.details.setHtml(
            "<h3>Как пользоваться</h3><ol>"
            "<li>Перетащите в окно трек или готовое видео перед загрузкой на YouTube, "
            "VK, Twitch или стриминг.</li>"
            "<li>Программа прослушает файл по кусочкам и сравнит каждый с базой Shazam.</li>"
            "<li>Красные участки на полосе — там звучит чужая записанная музыка, "
            "на которую может прийти заявка Content ID.</li></ol>"
            "<p>Нужен интернет. Shazam бесплатно принимает около трёх запросов в минуту, "
            "поэтому проверка идёт не мгновенно: трек на 3 минуты тщательно проверяется "
            "примерно за 4 минуты, быстро — примерно за 1 минуту. "
            "Найденные треки появляются сразу, не дожидаясь конца.</p>")

    def show_details(self) -> None:
        row = self.selected_row()
        if row is None:
            self.show_placeholder()
            return
        job = self.jobs[row]
        name = html.escape(os.path.basename(job["path"]))
        result = job["result"]
        partial = job.get("partial")
        self.timeline.set_result(result or partial)
        self.save_btn.setEnabled(result is not None)
        if result is not None:
            self.details.setHtml(f"<p style='color:gray'>{name}</p>" + result_html(result))
        elif job["status"] in ("running", "stopping") and partial is not None:
            self.details.setHtml(f"<p style='color:gray'>{name}</p>" + result_html(partial, job.get("wait", 0)))
        elif job["status"] == "failed":
            self.details.setHtml(
                f"<p style='color:gray'>{name}</p><h2>Проверка не выполнена</h2>"
                f"<p>{html.escape(job['error'])}</p>")
        elif job["status"] in ("running", "stopping"):
            self.details.setHtml(f"<p style='color:gray'>{name}</p><h2>Идёт проверка…</h2><p>Чтение файла</p>")
        else:
            self.details.setHtml(f"<p style='color:gray'>{name}</p><h2>В очереди</h2>")

    def save_report(self) -> None:
        row = self.selected_row()
        if row is None or self.jobs[row]["result"] is None:
            return
        result = self.jobs[row]["result"]
        default = str(Path(result.path).with_name(Path(result.path).stem + " — отчёт.txt"))
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить отчёт", default, "Текст (*.txt)")
        if path:
            # utf-8-sig, чтобы Блокнот на Windows точно открыл кириллицу
            Path(path).write_text(checker.report_text(result), encoding="utf-8-sig")

    def closeEvent(self, event):
        if self.worker:
            self.worker.stop_requested = True
            self.thread.quit()
            self.thread.wait(5000)
        event.accept()


STYLE = """
#dropZone {
    border: 2px dashed palette(mid);
    border-radius: 12px;
    background: palette(base);
}
#dropZone[active="true"] {
    border-color: palette(highlight);
    background: palette(alternate-base);
}
#updateBanner {
    border: 1px solid palette(highlight);
    border-radius: 8px;
    background: palette(alternate-base);
    margin: 6px 6px 0 6px;
}
"""


def selftest(report_path: str) -> int:
    """Проверка всего пути внутри собранного exe: поиск → отрывок → распознавание.

    Запуск: CopyrightCheck.exe --selftest отчёт.txt. Код выхода 0 — всё работает."""
    import tempfile
    import traceback
    import urllib.request

    lines = [f"Версия {__version__}, Python {sys.version.split()[0]}, прокси: {checker.system_proxy() or 'нет'}"]
    ok = True

    def step(name, func):
        nonlocal ok
        try:
            lines.append(f"[ok] {name}: {func()}")
        except Exception:
            ok = False
            lines.append(f"[FAIL] {name}:\n{traceback.format_exc()}")

    found = []
    step("поиск в Deezer", lambda: found.extend(search.search_tracks("Imagine Dragons Believer", 3))
         or f"{found[0].artist} — {found[0].title}, {found[0].label}")
    step("проверка обновлений", lambda: f"последняя версия {updater.latest_release().version}")

    def recognize():
        preview = Path(tempfile.gettempdir()) / "copyrightcheck_selftest.mp3"
        urllib.request.urlretrieve(found[0].preview, preview)
        result = checker.check_file(str(preview), "fast")
        preview.unlink(missing_ok=True)
        if not result.matches:
            raise RuntimeError(checker.report_text(result))
        return ", ".join(f"{m.track.artist} — {m.track.title}" for m in result.matches)

    if found:
        step("распознавание отрывка через Shazam", recognize)
    Path(report_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if ok else 1


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--selftest":
        sys.exit(selftest(sys.argv[2]))
    updater.cleanup_previous()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setStyleSheet(STYLE)
    window = MainWindow()
    window.show()
    # Файлы, брошенные на значок программы или открытые через «Открыть с помощью»
    if len(sys.argv) > 1:
        window.add_files(sys.argv[1:])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
