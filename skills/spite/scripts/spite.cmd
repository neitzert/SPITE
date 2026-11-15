@echo off
rem spite <step> <target> ...   (wrapper around spite.py for Windows cmd and PowerShell)
python -I "%~dp0spite.py" %*
