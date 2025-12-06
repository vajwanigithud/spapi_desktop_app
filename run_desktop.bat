@echo off
pushd %~dp0
call .venv_spapi\Scripts\activate.bat
python desktop.py
popd

