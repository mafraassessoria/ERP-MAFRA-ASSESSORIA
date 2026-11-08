@echo off
REM Abre o gerenciador como Administrador (o Windows vai pedir confirmacao).
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','\"%~dp0scripts\gerenciar.ps1\"','-Acao','remover'"
