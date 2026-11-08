"""Compatibilidade: o código foi para app/nfse/emissao.py."""
import sys
from .nfse import emissao as _m
sys.modules[__name__] = _m
