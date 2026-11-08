"""Senhas de usuários, criptografia dos certificados e bloqueio de tentativas de login."""
import hashlib
import hmac
import os
import time
from collections import defaultdict

from cryptography.fernet import Fernet

from .config import CHAVE_MESTRA

_fernet = Fernet(CHAVE_MESTRA)
_ITERACOES = 600_000


def gerar_hash_senha(senha: str) -> str:
    sal = os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", senha.encode(), sal, _ITERACOES)
    return f"pbkdf2_sha256${_ITERACOES}${sal.hex()}${h.hex()}"


def conferir_senha(senha: str, armazenado: str) -> bool:
    try:
        _, it, sal, h = armazenado.split("$")
        novo = hashlib.pbkdf2_hmac("sha256", senha.encode(), bytes.fromhex(sal), int(it))
        return hmac.compare_digest(novo.hex(), h)
    except Exception:
        return False


def senha_forte(senha: str) -> str | None:
    """Retorna mensagem de erro ou None se a senha for aceitável."""
    if len(senha) < 10:
        return "A senha precisa ter pelo menos 10 caracteres."
    if senha.isdigit() or senha.isalpha():
        return "Use letras e números na senha."
    return None


def criptografar(dados: bytes) -> bytes:
    return _fernet.encrypt(dados)


def descriptografar(dados: bytes) -> bytes:
    return _fernet.decrypt(dados)


# Bloqueio simples: 5 erros em 15 minutos bloqueiam o login/IP por 15 minutos.
_falhas: dict[str, list[float]] = defaultdict(list)
_JANELA = 15 * 60
_LIMITE = 5


def login_bloqueado(chave: str) -> bool:
    agora = time.time()
    _falhas[chave] = [t for t in _falhas[chave] if agora - t < _JANELA]
    return len(_falhas[chave]) >= _LIMITE


def registrar_falha(chave: str) -> None:
    _falhas[chave].append(time.time())


def limpar_falhas(chave: str) -> None:
    _falhas.pop(chave, None)
