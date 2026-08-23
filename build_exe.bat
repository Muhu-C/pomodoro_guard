@echo off
rem ============================================================
rem  Pomodoro Guard - one-click build script (onedir + windowed)
rem  Output : dist\PomodoroGuard\PomodoroGuard.exe
rem  NOTE   : distribute the whole dist\PomodoroGuard folder,
rem           not just the exe. Config file is created next to exe.
rem  Requires: PySide6 and PyInstaller installed (normal or --target
rem           install; script auto-detects local dep dirs, so no
rem           manual PYTHONPATH needed on this machine).
rem ============================================================
chcp 65001 >nul
cd /d %~dp0

rem ---- auto-detect local dependency dir (--target installs) ----
if not defined PYTHONPATH (
    if exist "%~dp0.pyside6"    set "PYTHONPATH=%~dp0.pyside6"
    if exist "%~dp0..\.pyside6" set "PYTHONPATH=%~dp0..\.pyside6"
)
if defined PYTHONPATH echo Using dependency dir: %PYTHONPATH%

rem ---- verify build dependencies ----
where python >nul 2>&1 || (echo [ERROR] python not found. Install Python and add it to PATH. & goto :err)
python -c "import PySide6" >nul 2>&1 || (echo [ERROR] PySide6 not found. Run: pip install PySide6 & goto :err)
python -c "import PyInstaller" >nul 2>&1 || (echo [ERROR] PyInstaller not found. Run: pip install pyinstaller & goto :err)
python -c "import win32gui" >nul 2>&1 || (
    echo [WARN] pywin32 not found. "工作时段最小化应用" feature will be disabled in the built exe.
    echo        Run: pip install pywin32
)
if not exist "%~dp0face_detection_yunet_2023mar.onnx" (
    echo [WARN] face_detection_yunet_2023mar.onnx not found.
    echo        Camera presence detection will be degraded in the built exe.
    echo        Download: https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx
)
if not exist "%~dp0person_det.onnx" (
    echo [WARN] person_det.onnx YOLOv8n person detector not found.
    echo        Bowed-head / back-to-camera presence detection will be disabled.
)

echo [1/2] Generating icon.ico ...
python make_icon.py || goto :err

echo [2/2] Building with PyInstaller (onedir + windowed) ...
python -m PyInstaller --noconfirm --clean --onedir --windowed ^
  --name PomodoroGuard ^
  --icon icon.ico ^
  --add-data "face_detection_yunet_2023mar.onnx;." ^
  --add-data "person_det.onnx;." ^
  --exclude-module PySide6.QtQml ^
  --exclude-module PySide6.QtQuick ^
  --exclude-module PySide6.QtWebEngineCore ^
  --exclude-module PySide6.QtWebEngineWidgets ^
  --exclude-module PySide6.QtNetwork ^
  --exclude-module PySide6.QtMultimedia ^
  --exclude-module PySide6.QtSql ^
  --exclude-module PySide6.QtTest ^
  --exclude-module PySide6.QtOpenGL ^
  --exclude-module PySide6.QtOpenGLWidgets ^
  main.py || goto :err

echo.
echo Build complete! Entry: dist\PomodoroGuard\PomodoroGuard.exe
echo NOTE: distribute the whole dist\PomodoroGuard folder together.
pause
exit /b 0

:err
echo.
echo Build FAILED. Check the messages above.
pause
exit /b 1
