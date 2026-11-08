@echo off
REM Liga o sistema ao GitHub e cria a tarefa de atualizacao automatica (executar UMA vez).
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','\"%~dp0scripts\instalar_atualizacao.ps1\"'"
