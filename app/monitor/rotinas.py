"""Rotinas de acompanhamento do e-CAC (substituto do eRadar), uma por assunto.

Cada rotina usa as consultas do módulo Receita Federal (serpro.servico.executar) — assim custo, limite mensal e
histórico ficam no mesmo lugar — e grava o resultado resumido em MonitorItem / PagamentoRF.

Os formatos de resposta do Serpro variam entre versões; por isso a leitura é tolerante (procura os campos pelo nome
em qualquer nível) e, quando não reconhece, grava status INDEFINIDO com o texto da Receita para conferência.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..serpro import servico as sv


# ---- utilitários de leitura ------------------------------------------------------------------

def dicts(obj):
    """Todos os dicionários dentro de obj, em qualquer nível."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from dicts(v)


def campo(d: dict, *nomes, padrao=None):
    low = {k.lower(): v for k, v in d.items()} if isinstance(d, dict) else {}
    for n in nomes:
        if n.lower() in low and low[n.lower()] not in (None, ""):
            return low[n.lower()]
    return padrao


def data_br(v) -> date | None:
    if v in (None, ""):
        return None
    s = str(v).strip()
    for fmt, n in (("%Y-%m-%d", 10), ("%d/%m/%Y", 10), ("%Y%m%d", 8)):
        try:
            return datetime.strptime(s[:n], fmt).date()
        except ValueError:
            continue
    return None


def periodo(v) -> str:
    """202610 / '2026-10' / '10/2026' → '2026-10'; '' se não reconhecer."""
    s = re.sub(r"\D", "", str(v or ""))
    if len(s) == 6:
        a, m = (s[:4], s[4:]) if s[:2] in ("19", "20") else (s[2:], s[:2])
        if 1 <= int(m) <= 12:
            return f"{a}-{m}"
    if len(s) == 8:
        d = data_br(v)
        return f"{d:%Y-%m}" if d else ""
    return ""


def dinheiro(v) -> Decimal:
    if v in (None, ""):
        return Decimal("0")
    s = str(v).strip()
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return Decimal(s).quantize(Decimal("0.01"))
    except InvalidOperation:
        return Decimal("0")


def mes_anterior(ref: date) -> str:
    d = ref.replace(day=1) - timedelta(days=1)
    return f"{d:%Y-%m}"


def gravar(db: Session, p: models.Prestador, item: str, ref: str, status: str, resumo: str, detalhe=None,
           consulta: models.ConsultaReceita | None = None) -> models.MonitorItem:
    m = db.scalar(select(models.MonitorItem).where(models.MonitorItem.prestador_id == p.id, models.MonitorItem.item == item,
                                                   models.MonitorItem.referencia == ref))
    if m is None:
        m = models.MonitorItem(prestador_id=p.id, item=item, referencia=ref)
        db.add(m)
    m.status, m.resumo = status, resumo[:300]
    m.detalhe = json.dumps(detalhe, ensure_ascii=False, default=str) if detalhe is not None else ""
    m.consulta_id = consulta.id if consulta is not None else None
    m.atualizado_em = datetime.now()
    db.flush()
    return m


def _dados(c: models.ConsultaReceita):
    try:
        return json.loads(c.dados) if c.dados else None
    except ValueError:
        return None


def _ref_minima(db) -> str:
    """Primeiro mês acompanhado, pela data de corte do Serpro (a apuração do mês anterior ao corte entra)."""
    corte = sv.data_corte(db)
    return (corte.replace(day=1) - timedelta(days=1)).strftime("%Y-%m") if corte else "0000-00"


def _consultar(db, p, codigo, valores, usuario, lote, cli):
    return sv.executar(db, p, codigo, valores, usuario, lote=lote, cli=cli)


def _nao_encontrado(texto: str) -> bool:
    """A Receita respondeu que não há o documento pedido (diferente de erro de acesso/procuração)."""
    return bool(re.search(r"n[ãa]o (foi |foram |há |ha |existe|possui)?\s*(encontrad|localizad|declara|registro|parcelamento|mensage|pagamento)"
                          r"|inexistente|nenhum[ao]? ", texto or "", re.I))


# ---- rotinas -------------------------------------------------------------------------------

def situacao_fiscal(db, p, usuario, lote, cli, hoje: date):
    c = _consultar(db, p, "sitfis", {}, usuario, lote, cli)
    if not c.ok or not c.lista_arquivos:
        return gravar(db, p, "SITFIS", "", "ERRO", c.mensagens or "Relatório não disponível", consulta=c)
    return gravar_sitfis(db, p, c)


def gravar_sitfis(db, p, c) -> models.MonitorItem:
    """Lê o relatório (PDF) da consulta e grava o resultado no Monitor. Também usado para reler PDFs já baixados."""
    caminho = sv.arquivo_absoluto(c.lista_arquivos[0]) if c.lista_arquivos else None
    r = ler_sitfis(caminho)
    return gravar(db, p, "SITFIS", "", r["status"], r["resumo"], r, c)


# ---- Relatório de Situação Fiscal (SITFIS) ---------------------------------------------------
# O PDF da Receita vem com a página girada; o texto é lido direto do conteúdo da página, linha a linha
# (blocos com a mesma altura formam uma linha). Os títulos de seção terminam em "_____".

_TJ = re.compile(rb"\((?:\\.|[^\\)])*\)\s*Tj")
_TM = re.compile(rb"([-\d.]+)\s+([-\d.]+)\s+Tm")


def _texto_pdf_string(b: bytes) -> str:
    b = b[b.index(b"(") + 1:b.rindex(b")")]
    b = re.sub(rb"\\([()\\])", rb"\1", b)
    return b.decode("cp1252", errors="replace")


def linhas_pdf(caminho) -> list[str]:
    if caminho is None:
        return []
    try:
        from pypdf import PdfReader
        paginas = PdfReader(str(caminho)).pages
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    for pg in paginas:
        try:
            dados = pg.get_contents().get_data()
        except Exception:  # noqa: BLE001
            continue
        y_ant, atual = None, []
        for bloco in re.findall(rb"BT(.*?)ET", dados, re.S):
            partes = [_texto_pdf_string(m.group(0)) for m in _TJ.finditer(bloco)]
            texto = "".join(partes).strip()
            if not texto:
                continue
            tm = _TM.search(bloco)
            y = tm.group(2) if tm else None
            if y is not None and y == y_ant:
                atual.append(texto)
            else:
                if atual:
                    out.append(" ".join(atual))
                atual, y_ant = [texto], y
        if atual:
            out.append(" ".join(atual))
    return [re.sub(r"\s+", " ", l).strip() for l in out]


_IGNORAR = re.compile(r"^(MINIST|SECRETARIA|PROCURADORIA|INFORMA|Por meio do|Autor pedido|P[áa]gina:|CNPJ:|CPF:|Final do Relat|\d\d/\d\d/\d{4} \d\d:)", re.I)
_NAO_SECAO = re.compile(r"^(Dados Cadastrais|S[óo]cios e Administradores|Certid[ãa]o Emitida)", re.I)


def ler_sitfis(caminho) -> dict:
    """Separa o relatório em seções e classifica cada uma:
    - pendência real (débito, omissão, inscrição, parcelamento em atraso…);
    - exigibilidade suspensa (informativa: não impede certidão positiva com efeitos de negativa);
    - base indisponível (a Receita não respondeu aquela base naquele momento — não é pendência).
    """
    linhas = linhas_pdf(caminho)
    r = {"status": "INDEFINIDO", "resumo": "", "pendencias": [], "detalhes": [], "suspensas": [],
         "indisponiveis": [], "certidao": "", "etiquetas": [], "cert_tipo": "", "cert_validade": ""}
    if not linhas:
        r["resumo"] = "Relatório baixado; leitura automática não concluiu — abrir o PDF"
        return r
    secoes, atual, bloco = [], None, ""
    for l in linhas:
        if re.match(r"^_{3,}", l):                       # separador de grupo (Diagnóstico na RFB / PGFN) ou fim
            bloco = re.sub(r"_+", " ", l).strip()
            atual = None
            continue
        m = re.match(r"^(.*?\S)\s*_{3,}\s*$", l)
        if m:
            titulo = m.group(1)
            atual = None if _NAO_SECAO.match(titulo) else {"titulo": titulo, "linhas": [], "grupo": bloco}
            if atual is not None:
                secoes.append(atual)
            elif titulo.lower().startswith("certid"):
                atual = {"certidao": True, "linhas": []}
            continue
        if atual is None or _IGNORAR.match(l):
            continue
        if atual.get("certidao"):
            if re.match(r"^Certid[ãa]o (Negativa|Positiva)", l, re.I) and not r["certidao"]:
                r["certidao"] = re.sub(r"\s{2,}", " ", l)
            continue
        atual["linhas"].append(l)
    for s in secoes:
        t = re.sub(r"^Pend[êe]ncia\s*[-–]\s*", "", s["titulo"])
        if any("BASE INDISPON" in x.upper() for x in s["linhas"]):
            r["indisponiveis"].append(t)
        elif re.search(r"exigibilidade suspensa", t, re.I):
            r["suspensas"].append(t)
        else:
            r["pendencias"].append(t)
            r["detalhes"].append({"titulo": t, "linhas": s["linhas"][:40]})
            r["etiquetas"].append(etiqueta_pendencia(t, s["linhas"]))
    sem_pend = re.search(r"n[ãa]o foram detectadas pend[êe]ncias", " ".join(linhas), re.I)
    cert = ""
    mc = re.match(r"Certid[ãa]o (Negativa|Positiva com Efeitos de Negativa|Positiva)", r["certidao"], re.I)
    if mc:
        cert = {"negativa": "CND", "positiva com efeitos de negativa": "CPEN", "positiva": "CPD"}.get(mc.group(1).lower(), "")
        r["cert_tipo"] = cert
        v = re.search(r"Validade:\s*(\d\d/\d\d/\d{4})", r["certidao"])
        if v:
            r["cert_validade"] = data_br(v.group(1)).isoformat() if data_br(v.group(1)) else ""
        cert = f"{cert} válida até {v.group(1)}" if v else cert
    extra = []
    if r["indisponiveis"]:
        extra.append(f"{len(r['indisponiveis'])} base(s) indisponível(is) na Receita")
    if cert:
        extra.append(cert)
    if r["pendencias"]:
        r["status"] = "PENDENCIA"
        r["resumo"] = f"{len(r['pendencias'])} pendência(s): " + ", ".join(r["pendencias"][:4])
    elif r["indisponiveis"]:
        r["status"] = "INDEFINIDO"
        r["resumo"] = "Nenhuma pendência encontrada, mas a Receita não respondeu todas as bases — consultar de novo em horário comercial"
        extra = [x for x in extra if "indispon" not in x]
    elif secoes or sem_pend:
        r["status"] = "REGULAR"
        r["resumo"] = "Sem pendências" + (f" ({len(r['suspensas'])} item(ns) com exigibilidade suspensa)" if r["suspensas"] else "")
    else:
        r["resumo"] = "Relatório baixado; leitura automática não concluiu — abrir o PDF"
    if extra:
        r["resumo"] += " · " + " · ".join(extra)
    r["resumo"] = r["resumo"][:300]
    return r


_DINHEIRO = re.compile(r"\b\d{1,3}(?:\.\d{3})*,\d{2}\b")


def _brl(v: Decimal) -> str:
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def etiqueta_pendencia(titulo: str, linhas: list[str]) -> dict:
    """Rótulo curto de uma pendência, com o número que importa (valor devido, parcelas em atraso, anos omissos)."""
    junto = " ".join(linhas)
    base = re.sub(r"\s*\((?:Sistema )?([A-Z/]+)\)", r" \1", titulo).strip()     # "Débito (SIEF)" -> "Débito SIEF"
    chave = base
    if re.search(r"^Parcelamento", titulo, re.I):
        chave = "Parcelamento"
        n = re.search(r"Parcelas em atraso\s+(\d+)", junto, re.I)
        sist = re.search(r"\(([^)]+)\)", titulo)
        texto = f"Parcelamento {sist.group(1) if sist else ''}".strip() + (f": {n.group(1)} em atraso" if n else "")
        return {"chave": chave, "texto": texto}
    if re.search(r"^Omiss", titulo, re.I):
        chave = base
        anos = sorted({a for a in re.findall(r"\b(20\d\d)\b", junto)})
        pas = re.findall(r"\b(\d\d/20\d\d)\b", junto)
        if anos and not pas:
            return {"chave": chave, "texto": f"{base} {', '.join(anos)}"}
        if pas:
            return {"chave": chave, "texto": f"{base} ({len(pas)} período(s))"}
        return {"chave": chave, "texto": base}
    linhas_valor = [l for l in linhas if _DINHEIRO.search(l)]
    if linhas_valor:
        total = Decimal("0")
        for l in linhas_valor:
            total += dinheiro(_DINHEIRO.findall(l)[-1])
        return {"chave": chave, "texto": f"{base} {_brl(total)}" + (f" ({len(linhas_valor)})" if len(linhas_valor) > 1 else "")}
    return {"chave": chave, "texto": base}


def ler_pdf(caminho) -> str:
    if caminho is None:
        return ""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        return "\n".join((pg.extract_text() or "") for pg in PdfReader(str(caminho)).pages)
    except Exception:  # noqa: BLE001
        return ""


def parcelamentos(db, p, usuario, lote, cli, hoje: date):
    codigo = "parcmei_pedidos" if p.regime == "MEI" else "parcsn_pedidos"
    c = _consultar(db, p, codigo, {}, usuario, lote, cli)
    if not c.ok:
        if _nao_encontrado(c.mensagens):
            return gravar(db, p, "PARCELAMENTO", "", "SEM", "Nenhum parcelamento", consulta=c)
        return gravar(db, p, "PARCELAMENTO", "", "ERRO", c.mensagens or "Sem resposta", consulta=c)
    lista = [dict(numero=campo(d, "numero", "numeroParcelamento"), situacao=campo(d, "situacao", padrao=""),
                  pedido=campo(d, "dataDoPedido", "dataPedido"), data_situacao=campo(d, "dataDaSituacao", "dataSituacao"))
             for d in dicts(_dados(c)) if campo(d, "numero", "numeroParcelamento") and campo(d, "situacao")]
    ativos = [x for x in lista if re.search(r"parcelamento|ativo|em dia|deferido", str(x["situacao"]), re.I)
              and not re.search(r"encerrad|rescind|liquidad|sem efeito|cancelad", str(x["situacao"]), re.I)]
    if not lista:
        return gravar(db, p, "PARCELAMENTO", "", "SEM", "Nenhum parcelamento", consulta=c)
    return gravar(db, p, "PARCELAMENTO", "", "COM" if ativos else "SEM",
                  f"{len(ativos)} em andamento de {len(lista)} pedido(s)", {"pedidos": lista}, c)


def caixa_postal(db, p, usuario, lote, cli, hoje: date):
    c = _consultar(db, p, "caixa_lista", {"status": "0"}, usuario, lote, cli)
    if not c.ok:
        if _nao_encontrado(c.mensagens):
            return gravar(db, p, "CAIXA", "", "SEM", "Nenhuma mensagem", consulta=c)
        return gravar(db, p, "CAIXA", "", "ERRO", c.mensagens or "Sem resposta", consulta=c)
    msgs = []
    for d in dicts(_dados(c)):
        isn = campo(d, "isn")
        if not isn:
            continue
        lida = str(campo(d, "indicadorLeitura", "statusLeitura", padrao="")).strip()
        msgs.append(dict(isn=str(isn), assunto=str(campo(d, "assuntoModelo", "assunto", padrao="")),
                         enviada=str(campo(d, "dataEnvio", padrao="")), remetente=str(campo(d, "remetente", "codigoSistemaRemetente", padrao="")),
                         lida=lida in ("1", "true", "True", "S", "Lida")))
    nao_lidas = sum(1 for m in msgs if not m["lida"])
    return gravar(db, p, "CAIXA", "", "NOVAS" if nao_lidas else "SEM",
                  f"{nao_lidas} não lida(s) de {len(msgs)}", {"mensagens": msgs[:100]}, c)


def pagamentos(db, p, usuario, lote, cli, hoje: date):
    inicio = (hoje.replace(day=1) - timedelta(days=1)).replace(day=1)
    c = _consultar(db, p, "pagamentos", {"de": inicio.isoformat(), "ate": hoje.isoformat()}, usuario, lote, cli)
    if not c.ok:
        if _nao_encontrado(c.mensagens):
            return gravar(db, p, "PAGAMENTOS", f"{hoje:%Y-%m}", "SEM", "Nenhum pagamento no período", consulta=c)
        return gravar(db, p, "PAGAMENTOS", f"{hoje:%Y-%m}", "ERRO", c.mensagens or "Sem resposta", consulta=c)
    novos = total = 0
    for d in dicts(_dados(c)):
        num = campo(d, "numeroDocumento")
        if not num or not campo(d, "valorTotal", "valor"):
            continue
        total += 1
        num = re.sub(r"\D", "", str(num)) or str(num)
        pg = db.scalar(select(models.PagamentoRF).where(models.PagamentoRF.prestador_id == p.id,
                                                        models.PagamentoRF.numero_documento == num))
        if pg is None:
            pg = models.PagamentoRF(prestador_id=p.id, numero_documento=num)
            db.add(pg)
            novos += 1
        tipo = campo(d, "tipo")
        pg.tipo = str(campo(tipo, "codigo", "descricao") if isinstance(tipo, dict) else (tipo or ""))[:40]
        rec = campo(d, "codigoReceitaDoc", "codigoReceita", "receitaPrincipal")
        pg.codigo_receita = str(campo(rec, "codigo") if isinstance(rec, dict) else (rec or ""))[:20]
        pa = campo(d, "periodoApuracao")
        pg.periodo_apuracao = data_br(pa) or (date.fromisoformat(periodo(pa) + "-01") if periodo(pa) else None)
        pg.vencimento = data_br(campo(d, "dataVencimento", "vencimento"))
        pg.data_arrecadacao = data_br(campo(d, "dataArrecadacao", "dataPagamento"))
        pg.valor = dinheiro(campo(d, "valorTotal", "valor"))
        pg.em_atraso = bool(pg.vencimento and pg.data_arrecadacao and pg.data_arrecadacao > pg.vencimento)
        pg.detalhe = json.dumps(d, ensure_ascii=False, default=str)[:4000]
    db.flush()
    return gravar(db, p, "PAGAMENTOS", f"{hoje:%Y-%m}", "COM" if total else "SEM",
                  f"{total} pagamento(s) desde {inicio:%d/%m/%Y}, {novos} novo(s)", None, c)


def pgdasd(db, p, usuario, lote, cli, hoje: date):
    pa = mes_anterior(hoje)
    ano = pa[:4]
    c = _consultar(db, p, "pgdasd_declaracoes", {"ano": ano}, usuario, lote, cli)
    if not c.ok and not _nao_encontrado(c.mensagens):
        return gravar(db, p, "PGDASD", pa, "ERRO", c.mensagens or "Sem resposta", consulta=c)
    entregues = set()
    for d in dicts(_dados(c)):
        per = periodo(campo(d, "periodoApuracao", padrao=""))
        if per and any(isinstance(v, (list, dict)) for v in d.values()):
            entregues.add(per)
    ultimo = None
    minimo = _ref_minima(db)
    for mes in range(1, int(pa[5:]) + 1):
        ref = f"{ano}-{mes:02d}"
        if ref < minimo:
            continue
        st = "ENTREGUE" if ref in entregues else "NAO_ENTREGUE"
        ultimo = gravar(db, p, "PGDASD", ref, st, "Declaração transmitida" if st == "ENTREGUE" else "Sem declaração na Receita",
                        consulta=c)
    return ultimo


def dctfweb(db, p, usuario, lote, cli, hoje: date):
    """Consulta o mês anterior e os meses ainda não entregues dos últimos 6 meses (no máximo 3 consultas extras)."""
    pa = mes_anterior(hoje)
    pendentes = db.scalars(select(models.MonitorItem.referencia).where(
        models.MonitorItem.prestador_id == p.id, models.MonitorItem.item == "DCTFWEB",
        models.MonitorItem.status.in_(("NAO_ENTREGUE", "ERRO")), models.MonitorItem.referencia < pa,
        models.MonitorItem.referencia >= max(f"{hoje - timedelta(days=186):%Y-%m}", _ref_minima(db)))).all()
    ultimo = None
    for ref in [pa] + sorted(pendentes, reverse=True)[:3]:
        c = _consultar(db, p, "dctf_recibo", {"categoria": "GERAL_MENSAL", "periodo": ref}, usuario, lote, cli)
        if c.ok:
            ultimo = gravar(db, p, "DCTFWEB", ref, "ENTREGUE", "Recibo de transmissão encontrado", consulta=c)
        elif _nao_encontrado(c.mensagens):
            ultimo = gravar(db, p, "DCTFWEB", ref, "NAO_ENTREGUE", c.mensagens[:200] or "Sem declaração", consulta=c)
        else:
            ultimo = gravar(db, p, "DCTFWEB", ref, "ERRO", c.mensagens or "Sem resposta", consulta=c)
    return ultimo


def mit(db, p, usuario, lote, cli, hoje: date):
    pa = mes_anterior(hoje)
    c = _consultar(db, p, "mit_lista", {"ano": pa[:4]}, usuario, lote, cli)
    if not c.ok and not _nao_encontrado(c.mensagens):
        return gravar(db, p, "MIT", pa, "ERRO", c.mensagens or "Sem resposta", consulta=c)
    sit = {}
    for d in dicts(_dados(c)):
        per = periodo(campo(d, "periodoApuracao", "periodo", padrao=""))
        if per:
            sit[per] = str(campo(d, "situacao", "situacaoApuracao", padrao="")).strip()
    ultimo = None
    minimo = _ref_minima(db)
    for mes in range(1, int(pa[5:]) + 1):
        ref = f"{pa[:4]}-{mes:02d}"
        if ref < minimo:
            continue
        s = sit.get(ref)
        st = "ENTREGUE" if s and re.search(r"encerrad|transmitid|entregue", s, re.I) else "NAO_ENTREGUE"
        ultimo = gravar(db, p, "MIT", ref, st, s or "Sem apuração encerrada", consulta=c)
    return ultimo


def defis(db, p, usuario, lote, cli, hoje: date):
    ano = str(hoje.year - 1)
    if ano < _ref_minima(db)[:4]:
        return None      # DEFIS do ano anterior ao corte não é acompanhada
    c = _consultar(db, p, "defis_lista", {}, usuario, lote, cli)
    if not c.ok and not _nao_encontrado(c.mensagens):
        return gravar(db, p, "DEFIS", ano, "ERRO", c.mensagens or "Sem resposta", consulta=c)
    anos = {str(campo(d, "anoCalendario", "ano", padrao="")) for d in dicts(_dados(c))}
    st = "ENTREGUE" if ano in anos else "NAO_ENTREGUE"
    return gravar(db, p, "DEFIS", ano, st, "Declaração transmitida" if st == "ENTREGUE" else "Sem DEFIS na Receita",
                  {"anos_entregues": sorted(a for a in anos if a)}, c)


# ---- quadro das rotinas -----------------------------------------------------------------------

@dataclass
class Rotina:
    codigo: str
    nome: str
    executar: Callable
    aplica: Callable[[models.Prestador], bool]
    servicos: tuple[str, ...]          # para a estimativa de custo
    agenda_padrao: str                 # S<dia semana> | M<dia do mês> | A<MM-DD>, seguido de HH:MM
    descricao: str


def _simples(p):
    return p.regime in ("SIMPLES", "SIMPLES_EXCESSO")


ROTINAS = [
    Rotina("SITFIS", "Situação fiscal", situacao_fiscal, lambda p: True, ("sitfis",), "M1 07:30",
           "Relatório de situação fiscal; lê o PDF e separa empresas regulares das com pendência."),
    Rotina("PARCELAMENTO", "Parcelamentos", parcelamentos, lambda p: p.regime != "REGULAR",
           ("parcsn_pedidos",), "M10 07:30", "Pedidos de parcelamento do Simples/MEI e a situação de cada um."),
    Rotina("CAIXA", "Caixa postal", caixa_postal, lambda p: True, ("caixa_lista",), "S0 07:30",
           "Mensagens da caixa postal do e-CAC (lidas e não lidas)."),
    Rotina("PAGAMENTOS", "Pagamentos", pagamentos, lambda p: True, ("pagamentos",), "M25 07:30",
           "Pagamentos do mês anterior e do atual (base para a baixa das guias)."),
    Rotina("PGDASD", "PGDAS-D", pgdasd, _simples, ("pgdasd_declaracoes",), "M21 07:30",
           "Declarações do Simples transmitidas no ano até o mês anterior."),
    Rotina("DCTFWEB", "DCTFWeb", dctfweb, lambda p: p.regime != "MEI", ("dctf_recibo",), "M16 07:30",
           "Recibo da DCTFWeb do mês anterior e dos meses ainda em aberto."),
    Rotina("MIT", "MIT", mit, lambda p: p.regime == "REGULAR", ("mit_lista",), "M28 07:30",
           "Apurações do MIT (empresas fora do Simples)."),
    Rotina("DEFIS", "DEFIS", defis, _simples, ("defis_lista",), "A04-01 07:30",
           "DEFIS do ano anterior (prazo 31 de março)."),
]
POR_ROTINA = {r.codigo: r for r in ROTINAS}
