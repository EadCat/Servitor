@echo off
rem Build a self-contained Servitor bundle into dist\, then wrap it in a
rem Windows installer. Run it from anywhere; it works out the repository root
rem itself.
rem
rem     build\build.bat                 build, creating .venv if it is missing
rem     build\build.bat /venv           recreate .venv and reinstall into it
rem     build\build.bat /clean          delete the generated files, then build
rem     build\build.bat /clean /venv    build from a clean tree, environment and all
rem
rem /clean removes dist\, .pyinstaller\ and the __pycache__ folders. It only
rem removes .venv when /venv is given too, so that a plain /clean cannot delete
rem the environment the build is about to run in.
rem
rem Everything runs out of .venv in the repository, made with the standard venv
rem module. conda is not used: Anaconda and Miniconda carry terms that make them
rem a paid product for many organisations, and a conda interpreter's python DLL
rem would be copied into the bundle. A venv cannot insulate the build from that,
rem since it shares the base interpreter's standard library.
rem
rem You do not need to deactivate a conda environment first; those interpreters
rem are simply skipped. Set PYTHON to name one explicitly.
rem
rem The installer step needs Inno Setup 6.6 or newer. Without it the folder
rem bundle is still built, and the script says how to get the compiler.
setlocal enabledelayedexpansion

pushd "%~dp0.."

set "DO_CLEAN="
set "DO_VENV="

:args
if "%~1"=="" goto :args_done
if /i "%~1"=="/clean" (
    set "DO_CLEAN=1"
    shift
    goto :args
)
if /i "%~1"=="/venv" (
    set "DO_VENV=1"
    shift
    goto :args
)
echo Unknown option: %~1
echo Usage: build\build.bat [/clean] [/venv]
goto :fail
:args_done

if defined DO_CLEAN (
    echo [clean] Removing the generated files...
    if exist "dist" rmdir /s /q "dist"
    if exist ".pyinstaller" rmdir /s /q ".pyinstaller"
    if exist "build\__pycache__" rmdir /s /q "build\__pycache__"
    if exist "src\__pycache__" rmdir /s /q "src\__pycache__"
    if defined DO_VENV if exist ".venv" rmdir /s /q ".venv"
)

rem Reuse an existing .venv only if it is still fit to build with. Checking now
rem avoids the old behaviour of finding out after pip had filled it.
call :vet_venv

if not defined DO_VENV goto :have_env

rem Try each candidate through :try_python, which sets BASEPY on success. The
rem py launcher comes first because it only ever finds python.org installs.
rem Written with goto rather than nested parentheses: inside a block, cmd
rem expands errorlevel before the call runs, so the check would read a stale
rem value. A failure prints everything it tried, which is what makes this
rem diagnosable at all.
set "BASEPY="
set "TRIED="

if defined PYTHON call :try_python "%PYTHON%"
if defined BASEPY goto :got_python
where py >nul 2>&1
if not errorlevel 1 call :try_python "py -3"
if defined BASEPY goto :got_python
call :try_python "python"
if defined BASEPY goto :got_python
call :try_python "python3"
if defined BASEPY goto :got_python

echo.
echo No usable interpreter found for building.
echo.
echo Tried:!TRIED!
echo.
echo It has to be a python.org install with tkinter, which is included by
echo default. A conda interpreter is skipped on purpose, because its python
echo DLL would be copied into the bundle; there is nothing to deactivate.
echo.
echo Install one:
echo     winget install -e --id Python.Python.3.12
echo.
echo then open a NEW terminal so PATH picks it up, and run:
echo     build\build.bat /venv
echo.
echo Or name one yourself:
echo     set PYTHON=C:\Path\To\python.exe ^&^& build\build.bat /venv
goto :fail

:got_python
echo       Creating .venv from !BASEPY!...
!BASEPY! -m venv ".venv"
if errorlevel 1 (
    echo.
    echo Could not create .venv with "!BASEPY! -m venv".
    goto :fail
)

call :find_python
if not defined PY (
    echo.
    echo .venv was created but no interpreter was found inside it.
    goto :fail
)

echo       Installing the requirements into .venv...
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :fail

:have_env
echo [1/3] Using %PY%

rem Check the runtime packages too, not just PyInstaller. Without them the
rem bundle still builds and then dies with ModuleNotFoundError when a user
rem runs it, which is a much worse place to find out.
"%PY%" -c "import PyInstaller, customtkinter, paramiko" 2>nul
if errorlevel 1 (
    echo.
    echo Build requirements are missing from %PY%.
    echo It needs PyInstaller plus everything in requirements.txt.
    echo.
    echo     build\build.bat /venv
    echo.
    echo sets up an environment with all of them, or install them yourself:
    echo     "%PY%" -m pip install -r requirements.txt pyinstaller
    goto :fail
)

rem The icons in assets\ are committed and packaged as they are.
echo [2/3] Building the bundle...
"%PY%" -m PyInstaller --noconfirm --clean build\Servitor.spec --workpath .pyinstaller --distpath dist
if errorlevel 1 goto :fail

echo [3/3] Compiling the installer...
rem Inno Setup does not put its compiler on PATH, so look where it installs to.
rem An unelevated winget install lands under %LOCALAPPDATA%, not Program Files.
set "ISCC="
for %%I in (ISCC.exe) do if not "%%~$PATH:I"=="" set "ISCC=%%~$PATH:I"
if not defined ISCC if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%LOCALAPPDATA%\Programs\Inno Setup 7\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 7\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 7\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 7\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 7\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 7\ISCC.exe"

if not defined ISCC (
    echo.
    echo Inno Setup was not found, so only the folder bundle was built:
    echo     dist\Servitor\Servitor.exe
    echo Install the compiler and run this script again:
    echo     winget install -e --id JRSoftware.InnoSetup
    goto :done
)

"%ISCC%" build\Servitor.iss
if errorlevel 1 goto :fail

echo.
echo Built the installer:
for %%F in ("dist\Servitor-Setup-*.exe") do echo     dist\%%~nxF

:done
echo.
echo Done.
popd
endlocal
exit /b 0

:find_python
rem venv puts the interpreter under Scripts; the other path covers a prefix
rem someone created another way.
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY if exist ".venv\python.exe" set "PY=.venv\python.exe"
goto :eof

:vet_venv
rem Decides whether .venv can be reused. Uses goto instead of nested blocks so
rem errorlevel is read after the call rather than before it.
call :find_python
if not defined PY goto :vet_fresh
call :check_python "%PY%"
if not errorlevel 1 goto :eof
echo The existing .venv cannot be used for building; recreating it.
rmdir /s /q ".venv"
set "PY="
set "DO_VENV=1"
goto :eof
:vet_fresh
echo No usable .venv here yet, so one will be created.
echo Any environment you already have is left untouched.
set "DO_VENV=1"
goto :eof

:try_python
rem Sets BASEPY when the candidate qualifies, and records what was tried so a
rem failure can say which interpreters were looked at.
call :check_python "%~1"
if errorlevel 1 (
    set "TRIED=!TRIED! %~1(no)"
    goto :eof
)
set "TRIED=!TRIED! %~1(ok)"
set "BASEPY=%~1"
goto :eof

:check_python
rem Sets errorlevel 0 only for a non-conda interpreter that can import tkinter.
rem tkinter is compiled into the interpreter, so pip cannot add it afterwards.
%~1 -c "import os,sys,sysconfig;b=sysconfig.get_config_var('prefix') or sys.base_prefix;sys.exit(1) if os.path.isdir(os.path.join(b,'conda-meta')) or 'conda' in sys.version.lower() else __import__('tkinter')" >nul 2>&1
goto :eof

:fail
echo.
echo Build failed.
popd
endlocal
exit /b 1
