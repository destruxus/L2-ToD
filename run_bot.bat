@echo off
REM =================================================================
REM L2 ToD Timer Bot - Interactive Startup Script for Windows
REM =================================================================
REM This script checks for necessary components, activates the
REM virtual environment, installs dependencies, and runs the bot.
REM It will ask for permission before making changes.
REM Place this file in the root of your project folder.
REM =================================================================

TITLE L2 ToD Timer Bot Launcher

:START
cls
ECHO.
ECHO  --- L2 ToD Timer Bot Interactive Launcher ---
ECHO.
ECHO  This script will check your setup and start the bot.
ECHO.

REM --- Step 1: Check for Python ---
ECHO [1/6] Checking for Python installation...
where python >nul 2>nul
if %errorlevel% neq 0 (
    ECHO [ERROR] Python is not found in your system's PATH.
    ECHO Please install Python (3.8+) and make sure it is added to your PATH.
    pause
    GOTO:END
)
ECHO [OK] Python found.

REM --- Step 2: Check for Bot Script ---
ECHO [2/6] Checking for bot.py...
if not exist "bot.py" (
    ECHO [CRITICAL ERROR] bot.py not found in this directory.
    ECHO Make sure this script is in the same folder as your main bot file.
    pause
    GOTO:END
)
ECHO [OK] Bot script found.

REM --- Step 3: Check for requirements.txt ---
ECHO [3/6] Checking for requirements.txt...
if not exist "requirements.txt" (
    ECHO [CRITICAL ERROR] requirements.txt not found.
    ECHO The bot cannot install its dependencies without this file.
    pause
    GOTO:END
)
ECHO [OK] Requirements file found.

REM --- Step 4: Check for Virtual Environment ---
ECHO [4/6] Checking for virtual environment (venv) folder...
if not exist "venv\Scripts\activate.bat" (
    ECHO [WARNING] The 'venv' folder was not found or is incomplete.
    CHOICE /C YN /M "Do you want me to try and create it for you now? [Y/N]"
    IF ERRORLEVEL 2 (
        ECHO User chose not to create the environment. Exiting.
        GOTO:END
    )
    IF ERRORLEVEL 1 (
        ECHO Creating virtual environment...
        python -m venv venv
        if %errorlevel% neq 0 (
            ECHO [ERROR] Failed to create the virtual environment.
            pause
            GOTO:END
        )
        ECHO [OK] Virtual environment created successfully.
    )
) else (
    ECHO [OK] Virtual environment found.
)


REM --- Step 5: Activate Virtual Environment and Install Requirements ---
ECHO [5/6] Activating virtual environment and installing dependencies...
call "venv\Scripts\activate.bat"
pip install -r requirements.txt
if %errorlevel% neq 0 (
    ECHO [ERROR] Failed to install requirements from requirements.txt.
    ECHO Please check the file and your internet connection.
    pause
    GOTO:END
)
ECHO [OK] Dependencies are up to date.

REM --- Step 6: Check for .env file (Warning only) ---
ECHO [6/6] Checking for .env file...
if not exist ".env" (
    ECHO [WARNING] .env file not found! The bot will likely fail to start.
    ECHO Make sure you have created a .env file with your DISCORD_BOT_TOKEN
    ECHO and DATABASE_ENCRYPTION_KEY.
) else (
    ECHO [OK] .env file found.
)

ECHO.
ECHO --- All checks passed. Starting the bot... ---
ECHO To stop the bot, press CTRL+C in this window.
ECHO.

python bot.py

:END
ECHO.
ECHO Script finished.
pause
