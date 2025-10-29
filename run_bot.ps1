# =================================================================
# L2 ToD Timer Bot - Interactive Startup Script for Windows PowerShell
# =================================================================
# This script checks for necessary components, activates the
# virtual environment, installs dependencies, and runs the bot.
# It will ask for permission before making changes.
# Place this file in the root of your project folder.
# =================================================================

# Set the title of the PowerShell window
$Host.UI.RawUI.WindowTitle = "L2 ToD Timer Bot Launcher"

Function Write-Step {
    param($Step, $Message)
    Write-Host -ForegroundColor Green "[${Step}/6] $Message"
}

Function Write-OK {
    param($Message)
    Write-Host -ForegroundColor Green "[OK] $Message"
}

Function Write-Warning {
    param($Message)
    Write-Host -ForegroundColor Yellow "[WARNING] $Message"
}

Function Write-Error {
    param($Message)
    Write-Host -ForegroundColor Red "[ERROR] $Message"
    Read-Host "Press Enter to exit."
    Exit
}

# --- Start of Script ---
Clear-Host
Write-Host "`n --- L2 ToD Timer Bot Interactive Launcher ---`n"
Write-Host " This script will check your setup and start the bot.`n"

# --- Step 1: Check for Python ---
Write-Step -Step 1 -Message "Checking for Python installation..."
$pythonPath = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonPath) {
    Write-Error "Python is not found in your system's PATH. Please install Python (3.8+) and ensure it's added to your PATH."
}
Write-OK "Python found."

# --- Step 2: Check for Bot Script ---
Write-Step -Step 2 -Message "Checking for bot.py..."
if (-not (Test-Path "bot.py")) {
    Write-Error "bot.py not found in this directory. Make sure this script is in the same folder as your main bot file."
}
Write-OK "Bot script found."

# --- Step 3: Check for requirements.txt ---
Write-Step -Step 3 -Message "Checking for requirements.txt..."
if (-not (Test-Path "requirements.txt")) {
    Write-Error "requirements.txt not found. The bot cannot install its dependencies without this file."
}
Write-OK "Requirements file found."

# --- Step 4: Check for Virtual Environment ---
Write-Step -Step 4 -Message "Checking for virtual environment (venv) folder..."
if (-not (Test-Path "venv\Scripts\Activate.ps1")) {
    Write-Warning "The 'venv' folder was not found or is incomplete."
    $choice = Read-Host "Do you want me to try and create it for you now? (y/n)"
    if ($choice -ne 'y') {
        Write-Host "User chose not to create the environment. Exiting."
        Read-Host "Press Enter to exit."
        Exit
    }
    
    Write-Host "Creating virtual environment..."
    python -m venv venv
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to create the virtual environment."
    }
    Write-OK "Virtual environment created successfully."
} else {
    Write-OK "Virtual environment found."
}


# --- Step 5: Activate Virtual Environment and Install Requirements ---
Write-Step -Step 5 -Message "Activating virtual environment and installing dependencies..."
try {
    . .\venv\Scripts\Activate.ps1
    pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to install requirements from requirements.txt. Please check the file and your internet connection."
    }
    Write-OK "Dependencies are up to date."
} catch {
    Write-Error "An error occurred while activating the environment or installing packages. Check your venv folder."
}

# --- Step 6: Check for .env file (Warning only) ---
Write-Step -Step 6 -Message "Checking for .env file..."
if (-not (Test-Path ".env")) {
    Write-Warning ".env file not found! The bot will likely fail to start."
    Write-Warning "Make sure you have created a .env file with your DISCORD_BOT_TOKEN and DATABASE_ENCRYPTION_KEY."
} else {
    Write-OK ".env file found."
}

Write-Host "`n--- All checks passed. Starting the bot... ---"
Write-Host "To stop the bot, press CTRL+C in this window."
Write-Host ""

# Run the bot
python bot.py

Read-Host "`nScript finished. Press Enter to close this window."

