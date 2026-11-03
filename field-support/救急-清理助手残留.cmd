@echo off
chcp 65001 >nul
title 千牛助手 残留进程清理
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0\救急-清理助手残留.ps1" -Force
