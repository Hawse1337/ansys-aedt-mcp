@echo off
setlocal
rem ---- Ansys AEDT MCP global launcher ----
set "AEDT_VERSION=2026.1"
set "AEDT_INSTALL_DIR=D:\ANSYS Inc\v261\AnsysEM"
set "AEDT_WORKER_TIMEOUT=1800"
set "AEDT_LAUNCH_TIMEOUT=240"
set "AEDT_LOG_DIR=%TEMP%\aedt-mcp"

set "MCP_HOME=%~dp0.."
"%MCP_HOME%\.venv\Scripts\python.exe" "%MCP_HOME%\mcp_server.py" %*
