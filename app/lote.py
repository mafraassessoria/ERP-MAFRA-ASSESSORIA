"""Compatibilidade: o código foi para app/nfse/lote.py."""
import sys
from .nfse import lote as _m
sys.modules[__name__] = _m
