@echo off
setlocal
call "%~dp0Start_servers_bitbucket.bat" %*
exit /b %errorlevel%
