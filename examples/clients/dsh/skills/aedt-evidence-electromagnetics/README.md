# AEDT evidence skill (fail-closed variant)

This is the DeepSeek Harness AEDT skill as patched after the guard-bypass incident: it
requires the guard MCP tools, stops when they are missing, forbids direct PyAEDT/nsysedt.exe
fallback, and no longer advises self-setting guard escape variables. Install by copying this
folder into the agent skill directory, e.g. ~/.dsh/skills/aedt-evidence-electromagnetics/.
