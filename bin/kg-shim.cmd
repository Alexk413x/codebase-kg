@echo off
rem The py launcher comes first: python and python3 may be Microsoft Store stubs on Windows.
set "SHIM=%~dp0..\mcp\src\codebase_kg\shim.py"
where py >nul 2>&1 && (py -3 "%SHIM%" %* & exit /b)
python "%SHIM%" %*
