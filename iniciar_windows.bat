@echo off
cd /d "%~dp0"
if not exist ".venv" (
    py -3 -m venv .venv
)
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt
echo Abrindo AutoClips AI Studio v0.6.0 em http://127.0.0.1:8765 ...
start "" http://127.0.0.1:8765
uvicorn server:app --host 127.0.0.1 --port 8765
