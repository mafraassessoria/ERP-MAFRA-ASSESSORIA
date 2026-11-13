"""Conferência do faturamento x receita declarada no PGDAS-D (empresas do Simples Nacional).

Faturamento do mês = NFS-e prestadas (do próprio ERP: emitidas + importadas do ADN)
                   + NF-e e NFC-e emitidas (NF-Stock, API MS-Exportação, com o código CRM).
Declarado = "Receita Bruta do PA (RPA)" e RBT12, lidos do PDF da última declaração PGDAS-D do período
            (Integra Contador CONSULTIMADECREC14 — consulta paga, feita só quando pedida).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from decimal import Decimal

import requests
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models

log = logging.getLogger(__name__)

EXPORTACAO = "https://ms-exportacao-nfstock.pack.alterdata.com.br"
SUBLIMITE = Decimal("3600000")
TETO = Decimal("4800000")
TOLERANCIA = Decimal("1.00")


class ErroConferencia(Exception):
    pass


def empresas(db: Session) -> list[models.Prestador]:
    return list(db.scalars(select(models.Prestador).where(models.Prestador.ativo.is_(True),
                                                          models.Prestador.regime == "SIMPLES")
                           .order_by(*models.ordem_prestador_sql())))


def periodo(ref: str) -> tuple[date, date]:
    ano, mes = (int(x) for x in ref.split("-"))
    ini = date(ano, mes, 1)
    fim = (date(ano + (mes == 12), mes % 12 + 1, 1) - timedelta(days=1))
    return ini, fim


def registro(db: Session, p: models.Prestador, ref: str) -> models.ConferenciaPGDAS:
    r = db.scalar(select(models.ConferenciaPGDAS).where(models.ConferenciaPGDAS.prestador_id == p.id,
                                                         models.ConferenciaPGDAS.referencia == ref))
    if r is None:
        r = models.ConferenciaPGDAS(prestador_id=p.id, referencia=ref)
        db.add(r)
    return r


# ---------------------------------------------------------------- faturamento

def nfse_do_mes(db: Session, p: models.Prestador, ref: str) -> tuple[Decimal, int]:
    ini, fim = periodo(ref)
    N = models.NotaFiscal
    data = func.coalesce(N.competencia, func.date(N.data_emissao))
    q = select(func.coalesce(func.sum(N.valor_servicos), 0), func.count()).where(
        N.prestador_id == p.id, N.papel == "PRESTADO", N.situacao == "AUTORIZADA", N.ambiente == "PRODUCAO",
        data >= ini.isoformat(), data <= fim.isoformat())
    v, n = db.execute(q).one()
    return Decimal(str(v or 0)).quantize(Decimal("0.01")), int(n or 0)


VARIANTES = (   # a documentação não fixa o formato; tenta e guarda a que o NF-Stock aceitar
    {"data": "%Y-%m-%d", "tipo": "emitida", "status": "autorizada"},
    {"data": "%Y-%m-%d", "tipo": "Emitida", "status": "Autorizada"},
    {"data": "%Y-%m-%d", "tipo": "EMITIDA", "status": "AUTORIZADA"},
    {"data": "%d/%m/%Y", "tipo": "emitida", "status": "autorizada"},
    {"data": "%Y-%m-%dT00:00:00", "tipo": "emitida", "status": "autorizada"},
    {"data": "%Y-%m-%d", "tipo": "emitida", "status": None},
    {"data": "%Y-%m-%d", "tipo": None, "status": None},
)
CHAVE_VARIANTE = "conferencia_nfstock_variante"


def _params(v: dict, ini: date, fim: date, pagina: int) -> dict:
    p = {"DataInicial": ini.strftime(v["data"]), "DataFinal": fim.strftime(v["data"]), "Tamanho": 200, "Pagina": pagina}
    if v["tipo"]:
        p["Tipo"] = v["tipo"]
    if v["status"]:
        p["Status"] = v["status"]
    return p


def _texto(r) -> str:
    try:
        return (r.text or "")[:200].replace("\n", " ")
    except Exception:  # noqa: BLE001
        return ""


def nfstock_emitidas(tk: str, crm: str, cnpj: str, tipo_doc: str, ref: str, sessao=None,
                     variante: int | None = None) -> tuple[Decimal, int, int]:
    """Soma as NF-e/NFC-e emitidas e autorizadas no mês (NF-Stock MS-Exportação). Devolve (total, quantidade, variante)."""
    s = sessao or requests
    ini, fim = periodo(ref)
    ordem = [variante] + [i for i in range(len(VARIANTES)) if i != variante] if variante is not None else range(len(VARIANTES))
    ultimo = ""
    for vi in ordem:
        v = VARIANTES[vi]
        total, n, pagina = Decimal("0"), 0, 1
        while True:
            r = s.get(f"{EXPORTACAO}/api/v1/{crm}/{cnpj}/{tipo_doc}", params=_params(v, ini, fim, pagina),
                      headers={"Authorization": f"Bearer {tk}", "Accept": "application/json"}, timeout=60)
            if r.status_code == 404:
                return total, n, vi
            if r.status_code == 400:
                ultimo = _texto(r)
                break          # formato não aceito: tenta a próxima variante
            if r.status_code in (401, 403):
                raise ErroConferencia(f"NF-Stock negou acesso a este CNPJ ({r.status_code}): {_texto(r) or 'empresa não vinculada à conta/CRM'}")
            if r.status_code >= 300:
                raise ErroConferencia(f"NF-Stock respondeu {r.status_code} ({tipo_doc}): {_texto(r)}")
            dados = r.json() or {}
            for d in dados.get("documentos") or []:
                st = str(d.get("status") or "autorizada").lower()
                emit = re.sub(r"\D", "", str(d.get("cpfcnpj_emitente") or cnpj))
                if st.startswith("autoriz") and emit[:8] == cnpj[:8]:
                    total += Decimal(str(d.get("valor") or 0))
                    n += 1
            if pagina >= int(dados.get("paginas") or 1):
                return total.quantize(Decimal("0.01")), n, vi
            pagina += 1
    raise ErroConferencia(f"NF-Stock não aceitou a consulta de {tipo_doc} (400): {ultimo}")


def atualizar_faturamento(db: Session, ref: str, sessao=None) -> dict:
    """Recalcula o faturamento de todas as empresas do Simples no mês (sem custo)."""
    from .configuracoes import ler_config
    from . import nfstock
    tk = nfstock.token(db)
    crm = (ler_config(db, nfstock.CHAVE_CRM) or "592534").zfill(6)
    from .configuracoes import gravar_config
    v = ler_config(db, CHAVE_VARIANTE)
    variante = int(v) if v.isdigit() and int(v) < len(VARIANTES) else None
    feitas, erros = 0, []
    for p in empresas(db):
        r = registro(db, p, ref)
        r.nfse, r.qtd_nfse = nfse_do_mes(db, p, ref)
        r.erro_nfstock = ""
        if tk:
            try:
                r.nfe, r.qtd_nfe, variante = nfstock_emitidas(tk, crm, p.cnpj, "nfe", ref, sessao, variante)
                r.nfce, r.qtd_nfce, variante = nfstock_emitidas(tk, crm, p.cnpj, "nfce", ref, sessao, variante)
                gravar_config(db, CHAVE_VARIANTE, str(variante))
            except (ErroConferencia, requests.RequestException, ValueError) as e:
                r.erro_nfstock = str(e)[:300]
                erros.append(f"{p.codigo_fmt}: {str(e)[:120]}")
        else:
            r.erro_nfstock = "token do NF-Stock não informado"
        r.faturamento_em = datetime.now()
        feitas += 1
        db.commit()
    return dict(feitas=feitas, erros=erros)


# ---------------------------------------------------------------- declarado (PGDAS-D)

_DIN = re.compile(r"\d{1,3}(?:\.\d{3})*,\d{2}")


def _valor(txt: str) -> Decimal:
    return Decimal(txt.replace(".", "").replace(",", "."))


def texto_pdf(caminho) -> str:
    from pypdf import PdfReader
    try:
        return "\n".join((pg.extract_text() or "") for pg in PdfReader(str(caminho)).pages)
    except Exception:  # noqa: BLE001
        return ""


def ler_declaracao(texto: str) -> dict:
    """Extrai RPA (receita do período) e RBT12 do texto da declaração PGDAS-D. Valores ausentes ficam None."""
    out = {"rpa": None, "rbt12": None}
    t = re.sub(r"[ \t]+", " ", texto or "")
    linhas = t.splitlines()
    for i, ln in enumerate(linhas):
        alvo = None
        if out["rpa"] is None and re.search(r"\(RPA\)", ln, re.I):
            alvo = "rpa"
        elif out["rbt12"] is None and re.search(r"\(RBT12\)", ln) and not re.search(r"RBT12p|proporcional", ln, re.I):
            alvo = "rbt12"
        if not alvo:
            continue
        valores = _DIN.findall(ln)
        j = i + 1
        while not valores and j < min(i + 4, len(linhas)):
            valores = _DIN.findall(linhas[j])
            j += 1
        if valores:
            out[alvo] = _valor(valores[-1])   # última coluna = total (mercado interno + externo)
    return out


CHAVE_PASTA = "conferencia_pasta_extratos"
PASTA_PADRAO = r"G:\Meu Drive\Rotinas Automáticas Fiscal"


def pasta_extratos(db: Session):
    """Pasta configurada; senão a cópia local (dados/extratos_pgdas, feita pela tarefa do usuário);
    senão G:\\Meu Drive\\Rotinas Automáticas Fiscal (o servidor roda como SYSTEM e em geral não enxerga o G:)."""
    from pathlib import Path
    from .config import DATA_DIR
    from .configuracoes import ler_config
    cfg = ler_config(db, CHAVE_PASTA)
    if cfg:
        return Path(cfg)
    local = DATA_DIR / "extratos_pgdas"
    return local if local.is_dir() else Path(PASTA_PADRAO)


def _pastas_empresas(base) -> dict[int, object]:
    """{código: pasta} a partir de nomes como "00094 - REPRESENTACOES AMS LTDA"."""
    out = {}
    try:
        for d in base.iterdir():
            m = re.match(r"\s*(\d+)\s*-", d.name)
            if d.is_dir() and m:
                out.setdefault(int(m.group(1)), d)
    except OSError:
        pass
    return out


def achar_extrato(pasta_empresa, ref: str):
    """Extrato do PGDAS-D da competência: <empresa>/MM-AAAA/extrato_pgdas_*.pdf (o mais recente = retificadora)."""
    if pasta_empresa is None:
        return None
    mm, aaaa = ref[5:], ref[:4]
    candidatos = []
    for nome in (f"{mm}-{aaaa}", f"{mm}.{aaaa}", f"{aaaa}-{mm}", f"{mm}{aaaa}"):
        d = pasta_empresa / nome
        if d.is_dir():
            candidatos += [a for a in d.glob("*.pdf") if "pgdas" in a.name.lower() and "extrato" in a.name.lower()]
    if not candidatos:   # nome do arquivo traz o período: extrato_pgdas_2026_10_01_2026_10_31.pdf
        candidatos = [a for a in pasta_empresa.rglob(f"*pgdas*{aaaa}_{mm}_*.pdf")]
    return max(candidatos, key=lambda a: a.stat().st_mtime) if candidatos else None


def ler_extratos(db: Session, ref: str) -> dict:
    """Lê RPA e RBT12 dos extratos do PGDAS-D salvos nas pastas das empresas (sem custo)."""
    base = pasta_extratos(db)
    if not base.is_dir():
        raise ErroConferencia(f"Pasta dos extratos não encontrada: {base}. Rode uma vez COPIAR_EXTRATOS_PGDAS.bat "
                              "(com o seu usuário, no servidor) para copiar os extratos do Drive para o ERP.")
    pastas = _pastas_empresas(base)
    lidos, sem = 0, []
    for p in empresas(db):
        try:
            cod = int(re.sub(r"\D", "", p.codigo_interno or "") or 0)
        except ValueError:
            cod = 0
        arq = achar_extrato(pastas.get(cod), ref)
        r = registro(db, p, ref)
        if arq is None:
            if r.declarado is None or r.arquivo_extrato:
                r.obs = "Extrato do PGDAS-D não encontrado na pasta da empresa."
            sem.append(p.codigo_fmt)
            continue
        texto = texto_pdf(arq)
        vals = ler_declaracao(texto)
        basico = re.search(r"CNPJ B[áa]sico:\s*([\d.]+)", texto)
        if basico and re.sub(r"\D", "", basico.group(1)) != p.cnpj[:8]:
            r.obs = f"O extrato da pasta é de outro CNPJ ({basico.group(1)}). Confira a pasta."
            sem.append(p.codigo_fmt)
            continue
        pa = re.search(r"Per[íi]odo de Apura[çc][ãa]o \(PA\):\s*(\d{2})/(\d{4})", texto)
        if pa and f"{pa.group(2)}-{pa.group(1)}" != ref:
            r.obs = f"O extrato encontrado é de {pa.group(1)}/{pa.group(2)}."
            sem.append(p.codigo_fmt)
            continue
        r.declarado, r.rbt12 = vals["rpa"], vals["rbt12"]
        r.arquivo_extrato, r.declarado_em, r.consulta_id = str(arq), datetime.now(), None
        r.obs = "" if vals["rpa"] is not None else "Extrato encontrado, mas o valor não foi lido."
        lidos += 1 if vals["rpa"] is not None else 0
    db.commit()
    return dict(lidos=lidos, sem=sem)


def ler_declarado(db: Session, p: models.Prestador, ref: str, usuario: str, cli=None) -> str:
    """Consulta a última declaração do período (paga) e grava os valores. Devolve "" ou o motivo."""
    from .serpro import servico as sv
    r = registro(db, p, ref)
    try:
        c = sv.executar(db, p, "pgdasd_ultima", {"periodo": ref}, usuario, cli=cli)
    except sv.ErroConsulta as e:
        return str(e)
    r.consulta_id, r.declarado_em = c.id, datetime.now()
    if not c.ok:
        r.obs = (c.mensagens or "Sem resposta")[:300]
        db.commit()
        return r.obs
    caminho = next((sv.arquivo_absoluto(a) for a in c.lista_arquivos if "decl" in a.lower()), None) \
        or (sv.arquivo_absoluto(c.lista_arquivos[0]) if c.lista_arquivos else None)
    vals = ler_declaracao(texto_pdf(caminho)) if caminho else {"rpa": None, "rbt12": None}
    r.declarado, r.rbt12 = vals["rpa"], vals["rbt12"]
    r.obs = "" if vals["rpa"] is not None else "Declaração baixada, mas o valor não foi lido — abra o PDF."
    db.commit()
    return "" if vals["rpa"] is not None else r.obs


# ---------------------------------------------------------------- tela

def situacao(r: models.ConferenciaPGDAS | None, entregue: str | None) -> tuple[str, str]:
    """(rótulo, classe) da linha."""
    if entregue == "NAO_ENTREGUE":
        return "Não declarado", "erro"
    if r is None or r.faturamento_em is None:
        return "Sem dados", ""
    if r.declarado is None:
        return "Declarado não lido", ""
    dif = (r.faturamento or 0) - r.declarado
    if abs(dif) <= TOLERANCIA:
        return "Confere", "ok"
    return ("Faturou mais que declarou" if dif > 0 else "Declarou mais que faturou"), "aviso"


def alerta_rbt12(v: Decimal | None) -> tuple[str, str]:
    if v is None:
        return "", ""
    if v > TETO:
        return "acima do teto do Simples", "erro"
    if v > SUBLIMITE:
        return "acima do sublimite (ICMS/ISS fora do DAS)", "erro"
    if v > SUBLIMITE * Decimal("0.9"):
        return "perto do sublimite", "aviso"
    return "", ""


def linhas(db: Session, ref: str) -> list[dict]:
    regs = {r.prestador_id: r for r in db.scalars(select(models.ConferenciaPGDAS)
                                                  .where(models.ConferenciaPGDAS.referencia == ref))}
    entregas = {m.prestador_id: m.status for m in db.scalars(select(models.MonitorItem).where(
        models.MonitorItem.item == "PGDASD", models.MonitorItem.referencia == ref))}
    out = []
    for p in empresas(db):
        r = regs.get(p.id)
        rot, cls = situacao(r, entregas.get(p.id))
        a, acls = alerta_rbt12(r.rbt12 if r else None)
        dif = (r.faturamento - r.declarado) if r and r.declarado is not None and r.faturamento_em else None
        out.append(dict(p=p, r=r, situacao=rot, classe=cls, dif=dif, alerta=a, alerta_cls=acls,
                        entregue=entregas.get(p.id, "")))
    return out
