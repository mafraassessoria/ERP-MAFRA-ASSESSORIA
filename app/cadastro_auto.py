"""Compatibilidade: o código foi para app/nfse/cadastro_auto.py."""
import sys
from .nfse import cadastro_auto as _m
sys.modules[__name__] = _m
