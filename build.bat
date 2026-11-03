@echo off
python -m venv .venv
call .venv\Scripts\activate
pip install -r requirements.txt
pyinstaller --noconfirm --onefile --windowed --name LocalAI --collect-all customtkinter --collect-all docx main.py
echo.
echo Pronto: dist\LocalAI.exe
pause
