@echo off
chcp 65001 >nul
title 千牛助手 现场诊断采集
echo 正在采集，请不要关闭窗口（约 30~60 秒）...
echo 不会重启千牛，也不会修改任何设置。
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0采集诊断.ps1"
echo.
pause
