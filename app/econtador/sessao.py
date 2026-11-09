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


# ---- login pelo próprio ERP (usuário, senha e código de verificação do e-Contador) --------------------
# Mapeado em 09/11/2026: formulário #form-login em POST "/" com CpfCNPJUsuario, Senha, CodigoDeAutenticacao,
# LembreMe e campos ocultos (UtilzaMfa, SenhaAux, RequerSelecaoBureau, RespostasLogins, UserConnectionId…).
# 1º envio (usuário+senha) → a página volta pedindo o código (UtilzaMfa=True); 2º envio com o código → logado.
# A senha não é guardada: só a sessão (cookies) que o e-Contador devolve.
import time as _time

import requests as _requests
from lxml import html as _html

from .web import BASE as _BASE

_PENDENTES: dict[int, dict] = {}
_VALIDADE_PENDENTE = 10 * 60


class ErroLogin(Exception):
    pass


def _form_login(texto: str) -> dict | None:
    arv = _html.fromstring(texto or "<html></html>")
    f = arv.xpath('//form[@id="form-login"]')
    if not f:
        return None
    campos = {}
    for el in f[0].xpath(".//input"):
        nome = el.get("name")
        if not nome:
            continue
        if el.get("type") == "checkbox":
            continue
        campos[nome] = el.get("value") or ""
    msgs = [" ".join(x.text_content().split()) for x in arv.xpath('//*[contains(@class,"validation-summary-errors") or contains(@class,"field-validation-error") or contains(@class,"alert-danger")]')]
    if campos.get("ModelMessage"):
        msgs.insert(0, campos["ModelMessage"])
    return dict(campos=campos, mensagens=[m for m in msgs if m])


def _sessao_nova() -> _requests.Session:
    s = SESSAO_HTTP_LOGIN() if callable(SESSAO_HTTP_LOGIN) else _requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ERP-MAFRA-ASSESSORIA", "Origin": _BASE, "Referer": _BASE + "/"})
    return s


SESSAO_HTTP_LOGIN = None   # testes trocam por uma fábrica de sessões falsas


def _exportar(s) -> list[dict]:
    out = []
    for c in s.cookies:
        out.append(dict(name=c.name, value=c.value, domain=c.domain or "econtador.alterdata.com.br", path=c.path or "/",
                        expires=c.expires or -1))
    return out


def _concluir(db: Session, s, origem: str) -> int:
    from .web import ClienteWeb
    cks = _exportar(s)
    n = ClienteWeb(cks, sessao=SESSAO_HTTP).testar() if SESSAO_HTTP is not None else ClienteWeb(cks).testar()
    gravar_cookies(db, cks, origem)
    registrar_teste(db, True, f"login pelo ERP, {n} empresa(s)")
    return n


def iniciar_login(db: Session, uid: int, usuario: str, senha: str) -> dict:
    """Devolve {"ok": True, "empresas": n} ou {"codigo": True, "aviso": "..."} ou {"escolher": [...]}."""
    usuario = "".join(ch for ch in (usuario or "") if ch.isalnum())
    if not usuario or not senha:
        raise ErroLogin("Informe o usuário (CPF/CNPJ) e a senha do e-Contador.")
    s = _sessao_nova()
    try:
        r0 = s.get(_BASE + "/", timeout=40)
        f0 = _form_login(r0.text)
        if not f0:
            raise ErroLogin("A tela de login do e-Contador não abriu como esperado.")
        dados = dict(f0["campos"], CpfCNPJUsuario=usuario, Senha=senha, LembreMe="true", CodigoDeAutenticacao="")
        r = s.post(_BASE + "/", data=dados, timeout=60)
    except _requests.RequestException as e:
        raise ErroLogin(f"Sem conexão com o e-Contador ({type(e).__name__}).") from e
    return _avaliar(db, uid, s, r, usuario, senha)


def _avaliar(db: Session, uid: int, s, r, usuario: str, senha: str) -> dict:
    f = _form_login(r.text)
    if f is None:                          # saiu da tela de login: logado
        _PENDENTES.pop(uid, None)
        return dict(ok=True, empresas=_concluir(db, s, "login pelo ERP"))
    c = f["campos"]
    if c.get("UtilzaMfa") == "True":
        p = _PENDENTES.get(uid) or {}
        _PENDENTES[uid] = dict(s=s, campos=c, usuario=usuario, senha=c.get("SenhaAux") or senha, em=_time.time(),
                               canal=p.get("canal", ""))
        aviso = f["mensagens"][0] if f["mensagens"] else ""
        if aviso or _PENDENTES[uid]["canal"]:
            return dict(codigo=True, aviso=aviso)       # código errado ou já enviado: pede de novo
        return dict(canais=negociar_canal(uid))          # o e-Contador só manda o código depois de escolher o canal
    if c.get("RequerSelecaoBureau") == "True" and c.get("RespostasLogins"):
        import json as _json
        try:
            opcoes = _json.loads(c["RespostasLogins"])
        except ValueError:
            opcoes = []
        _PENDENTES[uid] = dict(s=s, campos=c, usuario=usuario, senha=c.get("SenhaAux") or senha, em=_time.time(), opcoes=opcoes)
        return dict(escolher=[dict(id=str(o.get("userConnectionId") or ("no-data" if o.get("HasNoData") else "")),
                                   nome=str(o.get("Nome") or o.get("nome") or o.get("NomeBureau") or o.get("Descricao") or o))
                              for o in opcoes])
    raise ErroLogin(f["mensagens"][0] if f["mensagens"] else "O e-Contador não aceitou o login. Confira usuário e senha.")


def _pendente(uid: int) -> dict:
    p = _PENDENTES.get(uid)
    if not p or _time.time() - p["em"] > _VALIDADE_PENDENTE:
        _PENDENTES.pop(uid, None)
        raise ErroLogin("O login expirou. Comece de novo.")
    return p


def enviar_codigo(db: Session, uid: int, codigo: str) -> dict:
    p = _pendente(uid)
    codigo = (codigo or "").strip()
    if not codigo:
        raise ErroLogin("Digite o código de verificação.")
    dados = dict(p["campos"], CpfCNPJUsuario=p["usuario"], Senha=p["senha"], CodigoDeAutenticacao=codigo, LembreMe="true")
    try:
        r = p["s"].post(_BASE + "/", data=dados, timeout=60)
    except _requests.RequestException as e:
        raise ErroLogin(f"Sem conexão com o e-Contador ({type(e).__name__}).") from e
    return _avaliar(db, uid, p["s"], r, p["usuario"], p["senha"])


def escolher_acesso(db: Session, uid: int, conexao: str) -> dict:
    p = _pendente(uid)
    dados = dict(p["campos"], CpfCNPJUsuario=p["usuario"], Senha=p["senha"], UserConnectionId=conexao, LembreMe="true",
                 CodigoDeAutenticacao="")
    try:
        r = p["s"].post(_BASE + "/", data=dados, timeout=60)
    except _requests.RequestException as e:
        raise ErroLogin(f"Sem conexão com o e-Contador ({type(e).__name__}).") from e
    return _avaliar(db, uid, p["s"], r, p["usuario"], p["senha"])


def cancelar_login(uid: int) -> None:
    _PENDENTES.pop(uid, None)


def login_pendente(uid: int) -> bool:
    p = _PENDENTES.get(uid)
    return bool(p) and _time.time() - p["em"] <= _VALIDADE_PENDENTE


def _dados_canal(p: dict) -> dict:
    return {"CnpjBureau": p["campos"].get("CNPJEmpresa", ""), "CpfCnpj": p["usuario"], "Senha": p["senha"]}


def negociar_canal(uid: int) -> list[dict]:
    """Opções de envio do código (e-mail, WhatsApp, SMS) com o contato mascarado, como na tela do e-Contador."""
    p = _pendente(uid)
    try:
        r = p["s"].post(_BASE + "/api/cliente/negociarcanalmfa", data=_dados_canal(p), timeout=40)
        n = r.json() if r.status_code == 200 else {}
    except (_requests.RequestException, ValueError):
        n = {}
    g = lambda *k: next((n.get(x) for x in k if n.get(x) is not None), None)
    email, fone = g("EmailMascarado", "emailMascarado") or "", g("TelefoneMascarado", "telefoneMascarado") or ""
    opcoes = [dict(id="EMAIL", nome="E-mail", contato=email)]
    if g("WhatsappDisponivel", "whatsappDisponivel"):
        opcoes.append(dict(id="WHATSAPP", nome="WhatsApp", contato=fone))
    if g("SmsDisponivel", "smsDisponivel"):
        opcoes.append(dict(id="SMS", nome="SMS", contato=fone))
    return opcoes


def enviar_canal(uid: int, canal: str) -> None:
    p = _pendente(uid)
    if canal not in ("EMAIL", "WHATSAPP", "SMS"):
        raise ErroLogin("Escolha por onde receber o código.")
    try:
        r = p["s"].post(_BASE + "/api/cliente/dispararcanalmfa", data=dict(_dados_canal(p), Canal=canal), timeout=40)
    except _requests.RequestException as e:
        raise ErroLogin(f"Sem conexão com o e-Contador ({type(e).__name__}).") from e
    if r.status_code >= 400:
        raise ErroLogin("O e-Contador não conseguiu enviar o código. Tente outro canal ou aguarde alguns instantes.")
    p["canal"], p["em"] = canal, _time.time()
