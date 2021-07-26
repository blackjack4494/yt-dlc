@setlocal
@echo off
cd /d %~dp0..

if ["%~1"]==[""] (
    set "test_set="
) else if ["%~1"]==["core"] (
    set "test_set=-k "not download""
) else if ["%~1"]==["download"] (
    set "test_set=-k download"
) else (
    echo.Invalid test type "%~1". Use "core" ^| "download"
    exit /b 1
)

pytest %test_set%
