"""Compatibilidade: o código foi para app/nfse/importacao.py."""
import sys
from .nfse import importacao as _m
sys.modules[__name__] = _m
