@echo off
REM MareTF build script for Windows x64
REM Usage: build_maretf.bat [--gui] [--clean]
REM
REM Options:
REM   --gui    Build with GUI (requires Qt6)
REM   --clean  Clean build directory first

setlocal enabledelayedexpansion

set SCRIPT_DIR=%~dp0
set MARETF_DIR=%SCRIPT_DIR%maretf
set BUILD_DIR=%MARETF_DIR%\build
set BUILD_GUI=OFF
set CLEAN_BUILD=0

REM Parse arguments
:parse_args
if "%~1"=="" goto :done_args
if /i "%~1"=="--gui" (
    set BUILD_GUI=ON
    shift
    goto :parse_args
)
if /i "%~1"=="--clean" (
    set CLEAN_BUILD=1
    shift
    goto :parse_args
)
echo Unknown option: %~1
echo Usage: build_maretf.bat [--gui] [--clean]
exit /b 1
:done_args

REM Find CMake
set CMAKE_EXE=cmake
where cmake >nul 2>&1
if errorlevel 1 (
    REM Try Visual Studio CMake
    set CMAKE_EXE="C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
    if not exist !CMAKE_EXE! (
        set CMAKE_EXE="C:\Program Files\Microsoft Visual Studio\2022\Community\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
    )
    if not exist !CMAKE_EXE! (
        echo ERROR: CMake not found. Install CMake or Visual Studio 2022.
        exit /b 1
    )
)

echo === MareTF Build Script (Windows x64) ===
echo Build GUI: %BUILD_GUI%
echo.

REM Clean if requested
if %CLEAN_BUILD%==1 (
    if exist "%BUILD_DIR%" (
        echo Cleaning build directory...
        rmdir /s /q "%BUILD_DIR%"
    )
)

REM Create build directory
if not exist "%BUILD_DIR%" mkdir "%BUILD_DIR%"

REM Initialize MareTF submodules if needed
echo === Checking submodules ===
git -C "%MARETF_DIR%" submodule update --init --recursive --quiet
if errorlevel 1 (
    echo WARNING: Failed to initialize some submodules
)

REM Configure
echo === Configuring ===
%CMAKE_EXE% -S "%MARETF_DIR%" -B "%BUILD_DIR%" ^
    -G "Visual Studio 17 2022" -A x64 ^
    -DCMAKE_BUILD_TYPE=Release ^
    -DMARETF_BUILD_CLI=ON ^
    -DMARETF_BUILD_GUI=%BUILD_GUI% ^
    -DMARETF_BUILD_THUMBNAILER=OFF ^
    -DMARETF_BUILD_INSTALLER=OFF

if errorlevel 1 (
    echo ERROR: CMake configuration failed.
    exit /b 1
)

REM Build
echo.
echo === Building ===
%CMAKE_EXE% --build "%BUILD_DIR%" --config Release --target maretf -j

if errorlevel 1 (
    echo ERROR: Build failed.
    exit /b 1
)

echo.
echo === Build Complete ===
echo Binary: %BUILD_DIR%\Release\maretf.exe

REM Copy to tools directory
copy /y "%BUILD_DIR%\Release\maretf.exe" "%SCRIPT_DIR%maretf.exe" >nul 2>&1
if exist "%SCRIPT_DIR%maretf.exe" (
    echo Copied to: %SCRIPT_DIR%maretf.exe
)

endlocal
