"""Compatibilidade: o código foi para app/nfse/nfse_xml.py."""
import sys
from .nfse import nfse_xml as _m
sys.modules[__name__] = _m
