"""Senhas de usuários, criptografia dos certificados e bloqueio de tentativas de login."""
import hashlib
import hmac
import os
import time

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


# ---- Bloqueio de tentativas -------------------------------------------------------------------
# Contado a partir da auditoria (LOGIN_FALHOU), então vale mesmo depois de reiniciar o sistema.
# 5 erros em 15 minutos bloqueiam aquele login por 15 minutos; um login certo zera a contagem.
_JANELA = 15 * 60
_LIMITE = 5


def _detalhes_falha(login: str) -> tuple[str, str]:
    return f"login={login}", f"login={login} codigo"


def falhas_recentes(db, login: str) -> int:
    from datetime import datetime, timedelta
    from sqlalchemy import func, select
    from . import models
    desde = datetime.now() - timedelta(seconds=_JANELA)
    ultimo_ok = db.scalar(select(func.max(models.Auditoria.data_hora)).where(
        models.Auditoria.acao == "LOGIN", models.Auditoria.usuario_login == login))
    if ultimo_ok and ultimo_ok > desde:
        desde = ultimo_ok
    return db.scalar(select(func.count(models.Auditoria.id)).where(
        models.Auditoria.acao == "LOGIN_FALHOU", models.Auditoria.data_hora > desde,
        models.Auditoria.detalhes.in_(_detalhes_falha(login)))) or 0


def login_bloqueado(db, login: str) -> bool:
    return falhas_recentes(db, login) >= _LIMITE


# ---- Verificação em duas etapas (TOTP, RFC 6238 — Google Authenticator, Microsoft Authenticator…) ----
import base64 as _b64
import struct as _struct
from urllib.parse import quote as _quote

TOTP_PASSO = 30


def totp_novo_segredo() -> str:
    return _b64.b32encode(os.urandom(20)).decode().rstrip("=")


def _totp(segredo: str, passo: int) -> str:
    chave = _b64.b32decode(segredo + "=" * (-len(segredo) % 8), casefold=True)
    h = hmac.new(chave, _struct.pack(">Q", passo), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    return f"{(_struct.unpack('>I', h[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def totp_conferir(segredo: str, codigo: str, ultimo_passo: int = 0, agora: float | None = None) -> int:
    """Devolve o passo aceito (para impedir reuso do mesmo código) ou 0 se o código não confere."""
    codigo = "".join(ch for ch in (codigo or "") if ch.isdigit())
    if len(codigo) != 6 or not segredo:
        return 0
    atual = int((agora if agora is not None else time.time()) // TOTP_PASSO)
    for passo in (atual, atual - 1, atual + 1):   # tolera relógio do celular 30 s adiantado/atrasado
        if passo > ultimo_passo and hmac.compare_digest(_totp(segredo, passo), codigo):
            return passo
    return 0


def totp_uri(segredo: str, login: str) -> str:
    emissor = "ERP MAFRA ASSESSORIA"
    return f"otpauth://totp/{_quote(emissor)}:{_quote(login)}?secret={segredo}&issuer={_quote(emissor)}&digits=6&period={TOTP_PASSO}"


def qr_svg(texto: str) -> str:
    try:
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(texto, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
        return img.to_string(encoding="unicode")
    except Exception:  # noqa: BLE001 — sem a biblioteca: a chave em texto basta
        return ""


# ---- Código alternativo por e-mail (quando o celular não está à mão) ---------------------------------
EMAIL_CODIGO_MINUTOS = 10
EMAIL_CODIGO_TENTATIVAS = 5
EMAIL_CODIGO_INTERVALO = 60      # segundos entre dois envios


def email_mascarado(email: str) -> str:
    if "@" not in (email or ""):
        return ""
    nome, dom = email.split("@", 1)
    return (nome[:2] + "•" * max(len(nome) - 3, 2) + nome[-1:]) + "@" + dom


def email_codigo_novo() -> str:
    import secrets
    return f"{secrets.randbelow(1_000_000):06d}"
