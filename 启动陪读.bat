@echo off
rem 便携启动：优先用文件夹自带的 Python，没有就用系统的 pythonw（不弹黑窗，控制台一闪即退）
cd /d "%~dp0"
if not exist "%~dp0runtime\Python313\pythonw.exe" goto syspy
start "" "%~dp0runtime\Python313\pythonw.exe" "%~dp0peidu.py"
exit /b
:syspy
start "" pythonw "%~dp0peidu.py"
