@echo off
rem USB 에 담아 다니는 단일 실행 파일을 만든다.
rem 쓰지 않는 numpy/tkinter 를 빼면 62MB -> 48MB 가 된다.
rem 산출물은 Dropbox 밖에 만든다(동기화 부담을 피하려고).
set OUT=%USERPROFILE%\ScreenPen_build
python -m PyInstaller --noconfirm --noconsole --onefile ^
  --name PenOnMonitor ^
  --exclude-module numpy --exclude-module tkinter --exclude-module scipy ^
  --add-data "%~dp0logo.png;." ^
  --distpath "%OUT%\dist" --workpath "%OUT%\work" --specpath "%OUT%" ^
  "%~dp0screenpen_qt.py"
echo.
echo 결과: %OUT%\dist\PenOnMonitor.exe
pause
