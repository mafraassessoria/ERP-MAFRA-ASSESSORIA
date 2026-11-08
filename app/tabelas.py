"""Compatibilidade: o código foi para app/nfse/tabelas.py."""
import sys
from .nfse import tabelas as _m
sys.modules[__name__] = _m
