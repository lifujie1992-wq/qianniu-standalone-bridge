@echo off
chcp 65001 >nul
title 千牛 无障碍模式检查
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0检查无障碍模式.ps1"
