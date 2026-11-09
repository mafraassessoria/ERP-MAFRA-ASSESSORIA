"""Geração de guias pelo Integra Contador, substituição da anterior, entrega ao arquivador e baixa pelos pagamentos."""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..serpro import servico as sv

TIPOS = {
    # tipo: (rótulo, serviço do catálogo, regimes, é parcelamento)
    "DAS": ("DAS do Simples (vencido)", "pgdasd_das", ("SIMPLES", "SIMPLES_EXCESSO"), False),
    "DAS_MEI": ("DAS do MEI (vencido)", "pgmei_das", ("MEI",), False),
    "DARF_DCTFWEB": ("DARF da DCTFWeb", "dctf_guia", ("SIMPLES", "SIMPLES_EXCESSO", "REGULAR", "MEI"), False),
    "PARC_SN": ("Parcela do Simples", "parcsn_das", ("SIMPLES", "SIMPLES_EXCESSO"), True),
    "PARC_MEI": ("Parcela do MEI", "parcmei_das", ("MEI",), True),
}
SITUACOES = {"ABERTA": ("Em aberto", "aviso"), "PAGA": ("Paga", "ok"), "SUBSTITUIDA": ("Substituída", "")}
TIPO_PAGAMENTO = {"DAS": "DAS", "DAS_MEI": "DAS", "PARC_SN": "DAS", "PARC_MEI": "DAS", "DARF_DCTFWEB": "DARF"}

RE_NUM = re.compile(r"\b\d{2}\.\d{2}\.\d{5}\.\d{7}-\d\b")
RE_DINHEIRO = r"(?:\d{1,3}(?:\.\d{3})+|\d+),\d{2}"


def so_digitos(v) -> str:
    return re.sub(r"\D", "", str(v or ""))


def ler_guia(texto: str) -> dict:
    """Número do documento, valor total e 'pagar até' do PDF da guia (layout DAS/DARF da Receita)."""
    t = re.sub(r"\s+", " ", texto or "")
    out = {}
    m = RE_NUM.search(t)
    if m:
        out["numero"] = so_digitos(m.group(0))
    m = re.search(r"valor total do documento\s*:?\s*(" + RE_DINHEIRO + ")", t, re.I) \
        or re.search(r"valor total\s*:?\s*R?\$?\s*(" + RE_DINHEIRO + ")", t, re.I)
    if m:
        out["valor"] = Decimal(m.group(1).replace(".", "").replace(",", "."))
    m = re.search(r"pagar (?:este documento )?at[ée]\s*:?\s*(\d{2}/\d{2}/\d{4})", t, re.I) \
        or re.search(r"data de vencimento\s*:?\s*(\d{2}/\d{2}/\d{4})", t, re.I)
    if m:
        out["vencimento"] = datetime.strptime(m.group(1), "%d/%m/%Y").date()
    return out


def _dos_dados(dados) -> dict:
    """Mesmos campos, quando a API já os devolve no JSON."""
    from ..monitor.rotinas import campo, data_br, dicts, dinheiro
    out = {}
    for d in dicts(dados):
        n = campo(d, "numeroDocumento", "numeroDas", "numeroGuia")
        if n and "numero" not in out:
            out["numero"] = so_digitos(n)
        v = campo(d, "valorTotalDocumento", "valorTotal", "valor")
        if v is not None and not isinstance(v, (dict, list)) and "valor" not in out:
            out["valor"] = dinheiro(v)
        venc = campo(d, "dataLimiteAcolhimento", "dataVencimento", "dataValidade", "dataPagamento")
        if venc and "vencimento" not in out and data_br(venc):
            out["vencimento"] = data_br(venc)
    return out


def gerar(db: Session, p: models.Prestador, tipo: str, competencia: str, usuario: str,
          consolidacao: str = "", cli=None) -> models.GuiaRF:
    if tipo not in TIPOS:
        raise sv.ErroConsulta("Tipo de guia inválido.")
    rotulo, servico, regimes, _parc = TIPOS[tipo]
    if p.regime not in regimes:
        raise sv.ErroConsulta(f"{rotulo} não se aplica ao regime desta empresa.")
    valores = {"periodo": competencia, "consolidacao": consolidacao, "categoria": "GERAL_MENSAL"}
    c = sv.executar(db, p, servico, valores, usuario, cli=cli)
    if not c.ok or not c.lista_arquivos:
        raise sv.ErroConsulta(f"A Receita não gerou a guia: {c.mensagens or 'sem PDF na resposta'}")
    from ..monitor.rotinas import ler_pdf
    caminho = sv.arquivo_absoluto(c.lista_arquivos[0])
    info = _dos_dados(json.loads(c.dados) if c.dados else None)
    info.update({k: v for k, v in ler_guia(ler_pdf(caminho)).items() if v})
    g = models.GuiaRF(prestador_id=p.id, tipo=tipo, competencia=competencia, numero_documento=info.get("numero", ""),
                      valor=info.get("valor") or Decimal("0"), vencimento=info.get("vencimento"),
                      arquivo=c.lista_arquivos[0], gerada_por=usuario, consulta_id=c.id)
    if not g.numero_documento or not g.valor:
        g.erro = "Não foi possível ler número/valor do PDF; a baixa automática vai depender da conferência."
    for velha in db.scalars(select(models.GuiaRF).where(
            models.GuiaRF.prestador_id == p.id, models.GuiaRF.tipo == tipo, models.GuiaRF.competencia == competencia,
            models.GuiaRF.situacao == "ABERTA")):
        velha.situacao = "SUBSTITUIDA"
    db.add(g)
    db.flush()
    return g


def entregar(db: Session, g: models.GuiaRF) -> str:
    from ..arquivador import entregar as entregar_arq
    nome = f"GUIA-{(g.prestador.codigo_interno or 'x').zfill(2)}-{g.tipo}-{g.competencia}-{g.id}.pdf"
    destino = entregar_arq(db, sv.arquivo_absoluto(g.arquivo) or (sv.PASTA / "_inexistente"), nome)
    g.entregue_em = datetime.now()
    return destino


# ---- baixa ---------------------------------------------------------------------------------------

def conciliar(db: Session) -> dict:
    """Marca como pagas as guias em aberto que têm pagamento correspondente na Receita.

    1) número do documento igual  → paga;
    2) senão: mesma empresa, valor exato, pago até o vencimento da guia e tipo compatível → paga se for o único;
    3) mais de um candidato → fica para conferência (não baixa)."""
    abertas = db.scalars(select(models.GuiaRF).where(models.GuiaRF.situacao == "ABERTA")).all()
    usados = {pid for pid in db.scalars(select(models.GuiaRF.pagamento_id).where(models.GuiaRF.pagamento_id.is_not(None)))}
    pagas, conferir = 0, []
    for g in abertas:
        pgs = [x for x in db.scalars(select(models.PagamentoRF).where(models.PagamentoRF.prestador_id == g.prestador_id))
               if x.id not in usados]
        alvo, criterio = None, ""
        if g.numero_documento:
            iguais = [x for x in pgs if so_digitos(x.numero_documento) == g.numero_documento]
            if iguais:
                alvo, criterio = iguais[0], "número do documento"
        if alvo is None and g.valor:
            cand = [x for x in pgs if x.valor == g.valor
                    and (not g.vencimento or (x.data_arrecadacao and x.data_arrecadacao <= g.vencimento))
                    and (not x.tipo or TIPO_PAGAMENTO[g.tipo] in x.tipo.upper())
                    and (not x.data_arrecadacao or x.data_arrecadacao >= g.gerada_em.date())]
            if len(cand) == 1:
                alvo, criterio = cand[0], "valor e vencimento"
            elif len(cand) > 1:
                conferir.append(f"{g.prestador.razao_social} · {TIPOS[g.tipo][0]} {g.competencia}: {len(cand)} pagamentos possíveis")
        if alvo is not None:
            g.situacao, g.pagamento_id, g.pago_em, g.criterio_baixa = "PAGA", alvo.id, alvo.data_arrecadacao, criterio
            usados.add(alvo.id)
            pagas += 1
    db.flush()
    return {"pagas": pagas, "conferir": conferir}
