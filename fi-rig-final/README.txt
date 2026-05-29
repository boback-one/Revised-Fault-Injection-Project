FI RIG - ESP32-S3 Fault Injection Rig
======================================

REQUIREMENTS
  - Windows 10 or 11
  - Python 3.9 or newer  (https://python.org — check "Add Python to PATH")
  - An ESP32-S3 board connected via USB


HOW TO START THE HOST SOFTWARE
-------------------------------
1. Extract this zip/folder anywhere on your PC

2. Double-click:   START HERE.bat

That's it. It will:
  - Install all Python libraries automatically (first run only, ~2 minutes)
  - Start the web server
  - Open your browser to the FI Rig interface

Press Ctrl+C in the black window to stop.


HOW TO GET THE FIRMWARE (.bin file)
-------------------------------------
The firmware cannot be pre-built here — it needs the Espressif compiler.
Use GitHub Actions to build it for free, automatically:

  Step 1: Create a free account at https://github.com

  Step 2: Create a new repository called "fi-rig"
    - Click the + button top right → New repository
    - Name: fi-rig
    - Keep it Public (or Private)
    - Click Create repository

  Step 3: Upload the firmware folder to GitHub
    - Drag the entire "firmware" folder into your new repo
    - Also drag the ".github" folder
    - Commit the changes

  Step 4: Trigger a build
    - Click Actions tab in your repo
    - Click "Build Firmware" on the left
    - Click "Run workflow" → Run workflow

  Step 5: Download the .bin file
    - Wait ~3-5 minutes for the green checkmark
    - Click the completed workflow run
    - Download "fi-rig-firmware-esp32s3" from the Artifacts section
    - Inside the zip: use fi-rig-merged.bin

  Tip: If you push a tag (v1.0.0), it creates a GitHub Release automatically
       with the .bin file attached so you can always find it later.


HOW TO FLASH THE FIRMWARE
---------------------------
Once you have fi-rig-merged.bin:

  Option A - Browser (easiest, no install):
    1. Go to https://espressif.github.io/esptool-js/
    2. Click Connect and select your ESP32-S3 COM port
    3. Set offset to 0x0
    4. Choose file: fi-rig-merged.bin
    5. Click Program

  Option B - Command line:
    pip install esptool
    esptool.py --chip esp32s3 --port COM3 --baud 460800 write_flash 0x0 fi-rig-merged.bin
    (replace COM3 with your actual port from Device Manager)


HOW TO FIND YOUR COM PORT
--------------------------
  1. Connect the ESP32-S3 via USB
  2. Right-click Start → Device Manager
  3. Expand "Ports (COM & LPT)"
  4. Look for "USB Serial Device" or "CP210x" or "CH340"
  5. Note the COM number (e.g., COM3 or COM7)


TROUBLESHOOTING
---------------
  "python is not recognized"
    → Reinstall Python from https://python.org
    → During install, CHECK the box "Add Python to PATH"
    → Restart your PC after installing

  "pip install failed" / network error
    → Try a different network (corporate firewalls sometimes block pip)
    → Or: python -m pip install --index-url https://pypi.org/simple/ pyserial

  Browser opens but shows "Not connected to rig"
    → Normal — this means the software is running fine
    → Connect your ESP32-S3 and select the COM port in the UI

  "Access denied" on COM port
    → Close any other programs using the port (Arduino IDE monitor, PuTTY, etc.)
    → Only one program can use a COM port at a time

  ESP32 not showing in Device Manager
    → Install the CH340 driver: https://www.wch-ic.com/downloads/CH341SER_EXE.html
    → Or the CP210x driver: https://www.silabs.com/developers/usb-to-uart-bridge-vcp-drivers
    → Unplug and replug the USB cable after installing
