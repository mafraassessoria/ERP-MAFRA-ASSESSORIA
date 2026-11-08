"""Compatibilidade: o código foi para app/nfse/rotas_notas.py."""
import sys
from .nfse import rotas_notas as _m
sys.modules[__name__] = _m
