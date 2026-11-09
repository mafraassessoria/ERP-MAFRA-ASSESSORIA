"""Sessão do e-Contador pela tela: cookies do login humano (com verificação), guardados criptografados."""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy.orm import Session

from ..configuracoes import gravar_config, ler_config
from ..seguranca import criptografar, descriptografar
from .web import ClienteWeb, ErroSessao

SESSAO_HTTP = None   # testes trocam por uma sessão falsa


def gravar_cookies(db: Session, cookies: list[dict], origem: str = "") -> None:
    so = [c for c in cookies if "alterdata.com.br" in (c.get("domain") or "")]
    gravar_config(db, "econtador_web_cookies_cripto", criptografar(json.dumps(so).encode()).decode())
    gravar_config(db, "econtador_web_conectado_em", datetime.now().isoformat(timespec="minutes"))
    gravar_config(db, "econtador_web_origem", origem[:60])


def cookies(db: Session) -> list[dict]:
    v = ler_config(db, "econtador_web_cookies_cripto")
    if not v:
        return []
    try:
        return json.loads(descriptografar(v.encode()).decode())
    except Exception:  # noqa: BLE001
        return []


def esquecer(db: Session) -> None:
    gravar_config(db, "econtador_web_cookies_cripto", "")


def conectado_em(db: Session) -> str:
    v = ler_config(db, "econtador_web_conectado_em") or ""
    try:
        return datetime.fromisoformat(v).strftime("%d/%m/%Y %H:%M") if v else ""
    except ValueError:
        return ""


def cliente(db: Session) -> ClienteWeb:
    return ClienteWeb(cookies(db), sessao=SESSAO_HTTP)


def estado(db: Session) -> dict:
    return dict(tem=bool(cookies(db)), conectado_em=conectado_em(db), ultimo_teste=ler_config(db, "econtador_web_ultimo_teste") or "")


def registrar_teste(db: Session, ok: bool, msg: str) -> None:
    gravar_config(db, "econtador_web_ultimo_teste", f"{datetime.now():%d/%m/%Y %H:%M} · {'ok' if ok else 'falhou'} · {msg}"[:200])
    if not ok and isinstance(msg, str) and "venceu" in msg:
        pass


__all__ = ["gravar_cookies", "cookies", "cliente", "estado", "ErroSessao", "esquecer", "registrar_teste"]
