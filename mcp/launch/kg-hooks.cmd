@echo off
rem Starts the hook relay (shim.py --hooks) on Windows. See kg-hooks.
rem cmd.exe looks for py and python in the working folder before PATH, and a client runs
rem this in the project, which could plant one there. This turns that lookup off.
set NoDefaultCurrentDirectoryInExePath=1
rem The py launcher comes first: python and python3 may be Microsoft Store stubs on Windows.
set "SHIM=%~dp0..\src\codebase_kg\shim.py"
where py >nul 2>&1 && (py -3 -I -S "%SHIM%" --hooks & exit /b)
python -I -S "%SHIM%" --hooks
