"""Compatibilidade: o código foi para app/nfse/dps_extra.py."""
import sys
from .nfse import dps_extra as _m
sys.modules[__name__] = _m
