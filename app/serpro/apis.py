"""APIs avulsas do Serpro contratadas pelo escritório: Consulta CPF v3 e Consulta CND v1.

Configuração por API (Configurações → APIs do Serpro): modo, chaves (se vazias usa as do Integra
Contador), preço por chamada. O gasto entra no mesmo limite mensal do Integra Contador.
"""
from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models
from ..configuracoes import gravar_config, ler_config
from ..documentos import so_digitos, validar_documento
from ..seguranca import criptografar, descriptografar
from .gateway import ClienteGateway, ErroGateway

PRODUTOS = {"CPF": "Consulta CPF", "CND": "Consulta CND (certidão federal)"}
MODOS = {"desligado": "Desligado", "demonstracao": "Demonstração (dados fictícios, sem custo)",
         "producao": "Produção (cobra por chamada)"}
PRECOS_PADRAO = {"CPF": "0,00", "CND": "0,00"}
CODIGO_CND_PADRAO = "9001"
DIAS_CACHE_CPF = 180
SESSAO = None   # testes podem trocar por uma sessão falsa


class ErroAPI(Exception):
    """Pedido recusado antes de chamar o Serpro, ou falha de comunicação (nada cobrado)."""


# ---- configuração ------------------------------------------------------------------------

def modo(db: Session, produto: str) -> str:
    m = ler_config(db, f"serpro_{produto.lower()}_modo") or "desligado"
    return m if m in MODOS else "desligado"


def _dec(v: str, padrao: str = "0") -> Decimal:
    try:
        v = (v or padrao).strip()
        return Decimal(v.replace(".", "").replace(",", ".")) if "," in v else Decimal(v)
    except InvalidOperation:
        return Decimal(padrao.replace(",", "."))


def preco(db: Session, produto: str) -> Decimal:
    return _dec(ler_config(db, f"serpro_preco_{produto}") or PRECOS_PADRAO[produto], "0")


def codigo_cnd(db: Session) -> str:
    return (ler_config(db, "serpro_cnd_codigo") or CODIGO_CND_PADRAO).strip()


def gravar_chaves(db: Session, produto: str, ck: str, cs: str) -> None:
    p = produto.lower()
    if ck:
        gravar_config(db, f"serpro_{p}_ck_cripto", criptografar(ck.encode()).decode())
    if cs:
        gravar_config(db, f"serpro_{p}_cs_cripto", criptografar(cs.encode()).decode())


def _ler(db: Session, chave: str) -> str:
    v = ler_config(db, chave)
    return descriptografar(v.encode()).decode() if v else ""


def chaves(db: Session, produto: str) -> tuple[str, str, str]:
    """(ck, cs, origem) — as do produto; se não houver, as do Integra Contador."""
    p = produto.lower()
    ck, cs = _ler(db, f"serpro_{p}_ck_cripto"), _ler(db, f"serpro_{p}_cs_cripto")
    if ck and cs:
        return ck, cs, "próprias"
    ck, cs = _ler(db, "serpro_ck_cripto"), _ler(db, "serpro_cs_cripto")
    return ck, cs, ("as do Integra Contador" if ck and cs else "")


def cliente(db: Session, produto: str) -> ClienteGateway:
    m = modo(db, produto)
    if m == "desligado":
        raise ErroAPI(f"A {PRODUTOS[produto]} do Serpro está desligada. Ative em Configurações → APIs do Serpro.")
    ck, cs, _ = chaves(db, produto)
    try:
        return ClienteGateway(modo=m, consumer_key=ck, consumer_secret=cs, sessao=SESSAO)
    except ErroGateway as e:
        raise ErroAPI(str(e)) from e


def gasto_mes(db: Session, ref: date | None = None) -> Decimal:
    ref = ref or date.today()
    v = db.scalar(select(func.coalesce(func.sum(models.ConsultaAPI.custo), 0)).where(
        models.ConsultaAPI.data_hora >= datetime(ref.year, ref.month, 1), models.ConsultaAPI.modo == "producao"))
    return Decimal(str(v or 0))


def uso_mes(db: Session, produto: str, ref: date | None = None) -> tuple[int, Decimal]:
    ref = ref or date.today()
    filtro = (models.ConsultaAPI.data_hora >= datetime(ref.year, ref.month, 1), models.ConsultaAPI.modo == "producao",
              models.ConsultaAPI.produto == produto, models.ConsultaAPI.custo > 0)
    n = db.scalar(select(func.count(models.ConsultaAPI.id)).where(*filtro)) or 0
    v = db.scalar(select(func.coalesce(func.sum(models.ConsultaAPI.custo), 0)).where(*filtro))
    return int(n), Decimal(str(v or 0))


def _conferir_limite(db: Session, produto: str) -> None:
    if modo(db, produto) != "producao":
        return
    from . import servico as integra
    total = integra.gasto_mes(db)          # já inclui estas APIs
    if total + preco(db, produto) > integra.limite_mensal(db):
        raise ErroAPI(f"Limite de gasto mensal com o Serpro atingido (R$ {total:.2f}). Ajuste em Configurações → Receita Federal.")


def _registrar(db: Session, produto: str, doc: str, usuario: str, m: str, http: int, ok: bool, resumo: str,
               dados: dict | None, cobrada: bool) -> models.ConsultaAPI:
    c = models.ConsultaAPI(produto=produto, documento=doc, usuario=usuario[:40], modo=m, http=http, ok=ok,
                           resumo=resumo[:300], dados=json.dumps(dados or {}, ensure_ascii=False)[:20000],
                           custo=preco(db, produto) if (cobrada and m == "producao") else Decimal("0"))
    db.add(c)
    db.flush()
    return c


def _json(r) -> dict:
    try:
        d = r.json()
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {"lista": d}


# ---- Consulta CPF v3 ---------------------------------------------------------------------------

@dataclass
class ResultadoCPF:
    cpf: str
    nome: str = ""
    nome_social: str = ""
    situacao_codigo: str = ""
    situacao: str = ""
    nascimento: date | None = None
    inscricao: str = ""
    do_cache: bool = False
    consulta_id: int = 0

    @property
    def regular(self) -> bool:
        return self.situacao_codigo == "0"


def data_nascimento(v) -> date | None:
    """Aceita date, 'AAAA-MM-DD', 'DD/MM/AAAA' ou 'DDMMAAAA'."""
    if isinstance(v, date):
        return v
    v = (v or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d%m%Y", "%d-%m-%Y"):
        try:
            d = datetime.strptime(v, fmt).date()
            return d if date(1900, 1, 1) <= d <= date.today() else None
        except ValueError:
            continue
    return None


def _data_ddmmaaaa(v: str) -> date | None:
    try:
        return datetime.strptime(v or "", "%d%m%Y").date()
    except ValueError:
        return None


def _de_dados(cpf: str, d: dict, c: models.ConsultaAPI, cache: bool) -> ResultadoCPF:
    sit = d.get("situacao") or {}
    return ResultadoCPF(cpf=cpf, nome=(d.get("nome") or "").strip().upper(), nome_social=(d.get("nomeSocial") or "").strip(),
                        situacao_codigo=str(sit.get("codigo", "")), situacao=(sit.get("descricao") or "").strip(),
                        nascimento=_data_ddmmaaaa(d.get("nascimento", "")), inscricao=d.get("dataInscricao") or "",
                        do_cache=cache, consulta_id=c.id)


def consultar_cpf(db: Session, cpf: str, nascimento, usuario: str = "", forcar: bool = False) -> ResultadoCPF:
    cpf = so_digitos(cpf)
    if len(cpf) != 11 or not validar_documento(cpf):
        raise ErroAPI("CPF inválido (confira os dígitos).")
    nasc = data_nascimento(nascimento)
    if nasc is None:
        raise ErroAPI("Informe a data de nascimento: a Receita só devolve os dados do CPF com a data de nascimento correta.")
    m = modo(db, "CPF")
    if m == "desligado":
        raise ErroAPI("A Consulta CPF do Serpro está desligada. Ative em Configurações → APIs do Serpro.")
    if not forcar:   # mesmo CPF + nascimento consultado há pouco: usa o guardado, sem custo
        desde = datetime.now() - timedelta(days=DIAS_CACHE_CPF)
        for c in db.scalars(select(models.ConsultaAPI).where(
                models.ConsultaAPI.produto == "CPF", models.ConsultaAPI.documento == cpf, models.ConsultaAPI.ok.is_(True),
                models.ConsultaAPI.modo == m, models.ConsultaAPI.data_hora >= desde).order_by(models.ConsultaAPI.id.desc())):
            d = json.loads(c.dados or "{}")
            if d.get("nascimento") == nasc.strftime("%d%m%Y"):
                return _de_dados(cpf, d, c, True)
    _conferir_limite(db, "CPF")
    cli = cliente(db, "CPF")
    try:
        r = cli.cpf(cpf, nasc.strftime("%d%m%Y"))
    except ErroGateway as e:
        raise ErroAPI(str(e)) from e
    d = _json(r)
    if r.status_code == 200 and d.get("nome"):
        c = _registrar(db, "CPF", cpf, usuario, m, 200, True, f"{d.get('nome', '')} · {(d.get('situacao') or {}).get('descricao', '')}", d, True)
        return _de_dados(cpf, d, c, False)
    msg = _erro_cpf(r.status_code, d)
    _registrar(db, "CPF", cpf, usuario, m, r.status_code, False, msg, d, r.status_code in (404,))
    raise ErroAPI(msg)


def _erro_cpf(http: int, d: dict) -> str:
    texto = json.dumps(d, ensure_ascii=False).upper()
    if "DATA_NASCIMENTO_DIVERGENTE" in texto or "DIVERGENTE" in texto:
        return "A data de nascimento não confere com a da Receita para este CPF."
    if "DATA_NASCIMENTO_INVALIDA" in texto:
        return "Data de nascimento inválida."
    if "CPF_INVALIDO" in texto:
        return "CPF inválido para a Receita."
    if http == 404:
        return "CPF não encontrado na Receita."
    if http in (401, 403):
        return "O Serpro recusou o acesso à Consulta CPF (confira o contrato e as chaves)."
    if http == 429:
        return "Muitas consultas seguidas ao Serpro; aguarde alguns segundos."
    return f"A Consulta CPF não respondeu como esperado (HTTP {http})."


# ---- Consulta CND v1 ---------------------------------------------------------------------------

STATUS_CND = {
    1: "Já existia certidão válida (devolvida)", 2: "Certidão emitida", 3: "Certidão não emitida: há pendências na Receita/PGFN",
    4: "Contribuinte com situação que impede a emissão pela internet", 5: "Serviço da Receita indisponível; tente mais tarde",
    6: "Serviço da Receita indisponível; tente mais tarde", 7: "Em processamento na Receita",
    8: "Contribuinte não cadastrado na Receita", 99: "Erro na emissão",
}
NAO_COBRADOS = {5, 6, 7}
ESPERA_PROCESSAMENTO = (5, 10, 20)   # segundos entre as tentativas do Status 7


@dataclass
class ResultadoCND:
    documento: str
    status: int = 0
    mensagem: str = ""
    codigo_controle: str = ""
    tipo_certidao: str = ""
    emissao: date | None = None
    validade: date | None = None
    pdf: bytes = b""
    consulta_id: int = 0
    dados: dict = field(default_factory=dict)

    @property
    def tem_certidao(self) -> bool:
        return self.status in (1, 2) and bool(self.codigo_controle)


def _data_iso(v) -> date | None:
    v = str(v or "")[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def emitir_cnd(db: Session, documento: str, usuario: str = "", espera=time.sleep) -> ResultadoCND:
    """Emite (ou recupera a válida) a certidão conjunta federal (RFB/PGFN) de um CNPJ ou CPF."""
    doc = so_digitos(documento)
    if len(doc) not in (11, 14):
        raise ErroAPI("Documento inválido para a Consulta CND.")
    m = modo(db, "CND")
    _conferir_limite(db, "CND")
    cli = cliente(db, "CND")
    corpo = {"TipoContribuinte": 1 if len(doc) == 14 else 2, "ContribuinteConsulta": doc,
             "CodigoIdentificacao": codigo_cnd(db), "GerarCertidaoPdf": True}
    tentativas = list(ESPERA_PROCESSAMENTO)
    while True:
        try:
            r = cli.cnd(dict(corpo))
        except ErroGateway as e:
            raise ErroAPI(str(e)) from e
        d = _json(r)
        status = int(d.get("Status") or 0) if str(d.get("Status") or "").isdigit() else 0
        if status == 7 and tentativas:
            if d.get("Chave"):
                corpo["Chave"] = d["Chave"]
            espera(tentativas.pop(0))
            continue
        break
    cert = d.get("Certidao") or {}
    pdf_b64 = cert.get("DocumentoPdf") or ""
    msgs = "; ".join(str(x.get("Mensagem") or x.get("mensagem") or x) for x in (d.get("Mensagens") or []) if x)
    res = ResultadoCND(documento=doc, status=status, codigo_controle=re.sub(r"[^0-9A-Za-z]", "", cert.get("CodigoControle") or ""),
                       tipo_certidao=(cert.get("TipoCertidao") or "").strip(), emissao=_data_iso(cert.get("DataEmissao")),
                       validade=_data_iso(cert.get("DataValidade")))
    if r.status_code != 200 and not status:
        res.mensagem = {401: "O Serpro recusou o acesso à Consulta CND (confira o contrato e as chaves).",
                        403: "O Serpro recusou o acesso à Consulta CND (confira o contrato e as chaves).",
                        429: "Muitas consultas seguidas ao Serpro; aguarde."}.get(r.status_code, f"Consulta CND: HTTP {r.status_code}")
    else:
        res.mensagem = STATUS_CND.get(status, f"Status {status}: parâmetros recusados pela Receita") + (f" ({msgs})" if msgs else "")
    if pdf_b64:
        try:
            res.pdf = base64.b64decode(pdf_b64)
        except ValueError:
            res.pdf = b""
    guardar = {k: v for k, v in d.items() if k != "Certidao"}
    guardar["Certidao"] = {k: v for k, v in cert.items() if k != "DocumentoPdf"}
    cobrada = r.status_code == 200 and status not in NAO_COBRADOS and status != 0
    c = _registrar(db, "CND", doc, usuario, m, r.status_code, res.tem_certidao,
                   f"{res.mensagem}" + (f" · {res.tipo_certidao} até {res.validade:%d/%m/%Y}" if res.validade else ""), guardar, cobrada)
    res.consulta_id, res.dados = c.id, guardar
    return res
