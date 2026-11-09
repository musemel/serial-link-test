@echo off
setlocal
rem Set the local COM port. Match SIZE, COUNT, WAIT and XONXOFF on both sides.
set "COM_PORT=3"
set "SIZE=64"
set "COUNT=12"
set "WAIT=0.2"
set "XONXOFF=no"
python "%~dp0serial_link_test.py" --com %COM_PORT% --mode send --size %SIZE% --count %COUNT% --wait %WAIT% --xonxoff %XONXOFF%
exit /b %errorlevel%
