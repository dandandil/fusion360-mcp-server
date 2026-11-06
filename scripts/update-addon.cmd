@echo off
rem Copy the add-in from PC-108-7 into Fusion's AddIns folder, replacing the
rem old copy. Then in Fusion: Utilities > Add-Ins > Fusion360MCP > Stop, Run.
set "DEST=%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\Fusion360MCP"
if exist "%DEST%" rmdir /s /q "%DEST%"
scp -r -q PC-108-7:cad_experiments/fusion360-mcp-server/addon "%DEST%"
if errorlevel 1 (echo Copy failed & exit /b 1)
echo Add-in copied to %DEST%. Restart it in Fusion: Utilities ^> Add-Ins ^> Stop, Run.
