@echo off
rem MCP server for Claude Code: runs on PC-108-7, reaches the Fusion add-in
rem through a reverse ssh tunnel. A session left from a crash or reboot keeps
rem the port on PC-108-7, so it is released first (output must not reach the
rem MCP stdio, hence >nul).
ssh -o BatchMode=yes PC-108-7 "cad_experiments/fusion360-mcp-server/scripts/release-port.sh 9876" >nul 2>nul
ssh -T -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -R 9876:localhost:9876 PC-108-7 "cd ~/cad_experiments/fusion360-mcp-server && exec .venv/bin/fusion360-mcp-server"
