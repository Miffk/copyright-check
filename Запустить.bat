@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist ".venv\installed.ok" goto run

echo Первый запуск: устанавливаю нужные компоненты. Это займёт 1-3 минуты...
echo.
rem С Python 3.15 собранный exe не может обратиться к Shazam, берём 3.13 или 3.12, если есть
set "PY="
py -3.13 --version >nul 2>nul && set "PY=py -3.13"
if not defined PY py -3.12 --version >nul 2>nul && set "PY=py -3.12"
if not defined PY py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto nopython

%PY% -m venv .venv
if errorlevel 1 goto nopython
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto installfail
echo ok> ".venv\installed.ok"

:run
start "" ".venv\Scripts\pythonw.exe" app.py %*
exit /b 0

:nopython
echo Не найден Python.
echo Скачайте его с https://www.python.org/downloads/ и при установке
echo обязательно отметьте галочку "Add python.exe to PATH". Потом запустите этот файл снова.
echo.
pause
exit /b 1

:installfail
echo.
echo Не удалось установить компоненты. Проверьте интернет и запустите файл ещё раз.
rmdir /s /q .venv
pause
exit /b 1
