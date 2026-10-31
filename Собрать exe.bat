@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem Собирает один файл dist\CopyrightCheck.exe, который работает без установленного Python
if not exist ".venv\installed.ok" (
    echo Сначала один раз запустите "Запустить.bat", чтобы установить компоненты.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m pip install --disable-pip-version-check pyinstaller
if errorlevel 1 goto fail

".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name CopyrightCheck ^
    --collect-all shazamio_core ^
    --collect-all imageio_ffmpeg ^
    --exclude-module tkinter ^
    app.py
if errorlevel 1 goto fail

echo.
echo Готово: dist\CopyrightCheck.exe
echo Этот файл можно скопировать куда угодно и запускать двойным щелчком.
explorer dist
pause
exit /b 0

:fail
echo.
echo Сборка не удалась, подробности выше.
pause
exit /b 1
