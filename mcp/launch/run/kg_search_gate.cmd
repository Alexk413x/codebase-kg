:<<"::KG_CMD"
@echo off
rem codebase-kg hook launcher. One file runs in two shells (see hooks/README.md, "How a hook starts").
rem Windows PowerShell and cmd read this part. POSIX sh skips it as a here-document.
rem The py launcher comes first: python and python3 may be Microsoft Store stubs, so each is run first.
rem The script is found from this file's own folder, so no environment variable is needed.
rem `call exit /b %%errorlevel%%` hands the script's exit code back; a bare `exit /b` here returns 0.
rem No labels or goto: the file is stored with LF endings, which cmd.exe misreads around a label.
rem cmd.exe looks for py, python3 and python in the working folder before PATH, and a hook runs in
rem the project, which could plant one there. This turns that lookup off.
set NoDefaultCurrentDirectoryInExePath=1
py -3 -c "" >nul 2>&1
if not errorlevel 1 (py -3 "%~dp0..\..\..\hooks\kg_search_gate.py" & call exit /b %%errorlevel%%)
python3 -c "" >nul 2>&1
if not errorlevel 1 (python3 "%~dp0..\..\..\hooks\kg_search_gate.py" & call exit /b %%errorlevel%%)
python -c "" >nul 2>&1
if not errorlevel 1 (python "%~dp0..\..\..\hooks\kg_search_gate.py" & call exit /b %%errorlevel%%)
echo codebase-kg: no working Python interpreter (tried py -3, python3, python) 1>&2
exit /b 127
::KG_CMD
# POSIX sh (macOS, Linux, Git Bash) reads from here, because the hook runs `. <this file>`.
# The plugin folder is CLAUDE_PLUGIN_ROOT, else this file's own place (bash knows it), else the
# folder above `$call`, which the headers helper command sets to this file's path. Never the
# working folder: a hook runs in the project, which could plant a script there.
kg_root=${CLAUDE_PLUGIN_ROOT:-}
if [ -z "$kg_root" ] && [ -n "${BASH_SOURCE:-}" ]; then
  kg_root=$(CDPATH= cd -- "$(dirname -- "$BASH_SOURCE")/../../.." && pwd)
fi
if [ -z "$kg_root" ]; then
  case ${call:-} in
    */mcp/launch/run/*.cmd) kg_root=${call%/mcp/launch/run/*.cmd} ;;
  esac
fi
if [ -z "$kg_root" ]; then
  echo "codebase-kg: cannot find the plugin folder (CLAUDE_PLUGIN_ROOT is not set)" >&2
  exit 127
fi
# Each candidate must run: on Windows python3 can be a Microsoft Store stub. The script runs once.
for candidate in "py -3" python3 python; do
  if $candidate -c "" </dev/null >/dev/null 2>&1; then
    exec $candidate "$kg_root/hooks/kg_search_gate.py"
  fi
done
echo "codebase-kg: no working Python interpreter (tried py -3, python3, python)" >&2
exit 127
