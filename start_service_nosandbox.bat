@echo off
rem Start data platform service without sandbox (full network access)
rem NOTE: keep this file ASCII-only; Chinese comments break under cmd GBK codepage.
set "WORKBUDDY_DISABLE_AUTO_COLLECTION="
set "WORKBENCH_RUNTIME_READY=1"
"C:\Users\youruser\.workbuddy\binaries\python\versions\3.13.12.old.9068\python.exe" app_fastapi.py
