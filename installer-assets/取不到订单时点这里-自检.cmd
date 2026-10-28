@echo off
chcp 65001 >nul
title 千牛取订单自检
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0qn-order-check.ps1"
echo.
pause
