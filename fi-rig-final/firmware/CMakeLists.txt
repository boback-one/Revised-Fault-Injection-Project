cmake_minimum_required(VERSION 3.16)

# ── ESP-IDF CMake integration ─────────────────────────────────────────────────
# IDF_PATH must be set in your environment (done automatically by the
# ESP-IDF plugin in Eclipse after you configure the IDF path in:
#   Preferences → Espressif → ESP-IDF Configuration
include($ENV{IDF_PATH}/tools/cmake/project.cmake)

project(fault_injector
    VERSION 1.0.0
    DESCRIPTION "ESP32-S3 Fault Injection Rig"
    LANGUAGES C
)
