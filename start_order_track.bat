@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Creando entorno virtual...
  python -m venv .venv
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
) else (
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
)
echo Abriendo http://127.0.0.1:8787
start "" http://127.0.0.1:8787
".venv\Scripts\python.exe" -m uvicorn web.app:app --host 127.0.0.1 --port 8787
