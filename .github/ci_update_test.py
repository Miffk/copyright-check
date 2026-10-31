"""Проверка самообновления на Windows: подмена запущенного .exe и запуск нового.

Запускает собранный exe, пока он работает — вызывает updater.install_and_restart
так же, как это делает окно, и проверяет, что новый файл встал на место и запустился.
"""

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import updater  # noqa: E402

built = Path(sys.argv[1]).resolve()
folder = Path(tempfile.mkdtemp())
exe = folder / "CopyrightCheck.exe"
shutil.copy(built, exe)
updater.current_exe = lambda: exe

running = subprocess.Popen([str(exe)])
time.sleep(15)
assert running.poll() is None, "старая версия не запустилась"

new_file = exe.with_suffix(".download")
shutil.copy(built, new_file)
updater.install_and_restart(new_file)
assert exe.exists() and exe.with_suffix(".old.exe").exists(), list(folder.iterdir())
print("файл подменён:", sorted(p.name for p in folder.iterdir()))

running.kill()  # так окно закрывается после обновления
time.sleep(15)
listing = subprocess.run(
    ["powershell", "-NoProfile", "-Command",
     "Get-Process CopyrightCheck -ErrorAction SilentlyContinue | "
     "ForEach-Object { $_.Path + ' | ' + $_.MainWindowTitle }"],
    capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
print(listing)
assert str(exe).lower() in listing.lower(), "новая версия не запустилась"
print("новая версия запущена")
subprocess.run(["taskkill", "/f", "/im", "CopyrightCheck.exe"], capture_output=True)
