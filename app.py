"""Окно программы «Проверка авторских прав»."""

from __future__ import annotations

import html
import math
import os
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QRectF, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout,
    QHeaderView, QLabel, QMainWindow, QMessageBox, QPushButton, QSplitter,
    QTableWidget, QTableWidgetItem, QTextBrowser, QToolTip, QVBoxLayout, QWidget,
)

import checker

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
    if not result.matches:
        parts.append(f"<h2 style='color:{COLOR_CLEAN.name()}'>✓ Совпадений не найдено</h2>")
        parts.append(
            "<p>Ни один фрагмент не совпал с известными записями. Это хороший знак, "
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
            f"из {len(result.segments)} (серые участки на полосе). Проверьте интернет "
            "и запустите проверку ещё раз.</p>")
    parts.append(
        f"<p style='color:gray'>Длительность {checker.format_time(result.duration)}, "
        f"проверено фрагментов: {len(result.segments)}.</p>")
    return "".join(parts)


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

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(self.drop)
        layout.addLayout(controls)
        layout.addWidget(splitter, 1)
        layout.addWidget(disclaimer)
        self.setCentralWidget(central)
        self.show_placeholder()

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
"""


def main() -> None:
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
