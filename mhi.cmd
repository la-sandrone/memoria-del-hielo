@echo off
REM ===========================================================================
REM  mhi.cmd - Memoria del Hielo CLI forwarder (native Windows Python)
REM
REM  Usage:  mhi <subcommand> [args...]
REM          mhi import "D:\path\to\book.md" --bucket game
REM          mhi rm --guid 1006bb53-974d-4edd-aa8c-3e72d1cfd8a0 --dry-run
REM          mhi help          (Chinese help text comes from the Python side)
REM
REM  Notes:
REM   * Args are passed verbatim to src\cli\mhi_dispatch.py.
REM   * Current directory is NOT changed: relative path args resolve against
REM     the directory you typed the command in (config.toml is found via the
REM     Config._find_config() __file__ fallback, so cwd does not matter).
REM   * Override interpreter with:  set MHI_PYTHON=py -3.12
REM   * ASCII-only on purpose: cmd.exe decodes batch lines with the CURRENT
REM     code page, so non-ASCII bytes in comments can be mis-decoded.
REM
REM  NO chcp HERE -- on purpose. Encoding is Python's job:
REM   * Attached to a real console, CPython uses _io.WindowsConsoleIO
REM     (PEP 528, Python 3.6+): UTF-8 at the byte layer, converted to UTF-16
REM     for ReadConsoleW/WriteConsoleW -- the console code page is bypassed.
REM     Measured here: console CP 936, isatty=True, stdout.encoding=utf-8,
REM     Chinese and emoji render correctly.
REM   * When stdout is piped/redirected there is no console, so Python falls
REM     back to the locale encoding (gbk here) and printing e.g. U+26A0 raises
REM     UnicodeEncodeError. -X utf8 (UTF-8 Mode, PEP 540) pins stdio to UTF-8
REM     and sets surrogateescape error handlers.
REM   * chcp would (a) clear the screen and (b) mutate shared console state.
REM     Not acceptable for a command that just prints help.
REM   * Caveat: a globally set PYTHONIOENCODING outranks -X utf8 (PEP 540).
REM ===========================================================================
setlocal EnableExtensions

if not defined MHI_PYTHON set "MHI_PYTHON=python"
"%MHI_PYTHON%" -X utf8 "%~dp0src\cli\mhi_dispatch.py" %*
set "_MHI_RC=%ERRORLEVEL%"

endlocal & exit /b %_MHI_RC%
