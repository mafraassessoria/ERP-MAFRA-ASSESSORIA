"""Gravações no e-Contador pela tela: baixa de honorários, envio/baixa/exclusão de guias.

Regras: nada é gravado sem conferir antes (empresa, título, valor, período) e tudo é relido depois.
Se a resposta não for conclusiva, o item fica com erro "conferir no e-Contador" e não é repetido sozinho.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..configuracoes import ler_config
from . import sessao
from .web import CATEGORIA_GUIAS, ErroSessao, ErroWeb

SITUACOES_REVISAO = {
    "PRONTO": ("Pronto para baixar", "ok"), "JA_PAGO": ("Já está pago no e-Contador", ""),
    "OUTRA_DATA": ("Pago no e-Contador com outra data", "aviso"), "NAO_ENCONTRADO": ("Cobrança não encontrada", "erro"),
    "VARIOS": ("Mais de uma cobrança com esse título", "erro"), "DIVERGENTE": ("Valor ou período diferente", "erro"),
    "PARCIAL": ("Pagamento parcial: baixar à mão", "erro"), "FEITO": ("Baixa já feita pelo ERP", "ok"),
    "DATA": ("Data de pagamento inválida", "erro"),
}


def automatico(db: Session, chave: str) -> bool:
    return (ler_config(db, chave) or "0") == "1"


# ---- honorários -----------------------------------------------------------------------------

def revisar_honorarios(db: Session, ids: list[int], cli=None) -> list[dict]:
    cli = cli or sessao.cliente(db)
    saida = []
    for rec in db.scalars(select(models.Recebimento).where(models.Recebimento.id.in_(ids)).order_by(models.Recebimento.codigo_empresa)):
        item = dict(rec=rec, doc=None, situacao="", detalhe="")
        saida.append(item)
        if rec.ec_baixado_em:
            item["situacao"], item["detalhe"] = "FEITO", f"em {rec.ec_baixado_em:%d/%m/%Y %H:%M}"
            continue
        if not rec.pagamento or rec.pagamento > date.today():
            item["situacao"] = "DATA"
            continue
        if rec.recebido and rec.valor and rec.recebido != rec.valor:
            item["situacao"], item["detalhe"] = "PARCIAL", f"recebido R$ {rec.recebido} de R$ {rec.valor}"
            continue
        docs = cli.localizar_cobranca(rec.titulo, rec.codigo_empresa)
        if not docs:
            item["situacao"] = "NAO_ENCONTRADO"
            continue
        if len(docs) > 1:
            item["situacao"], item["detalhe"] = "VARIOS", ", ".join(d.id for d in docs)
            continue
        d = item["doc"] = docs[0]
        ini, fim = d.periodo
        if d.valor != rec.valor or (rec.periodo_inicio and ini and ini != rec.periodo_inicio) or (rec.periodo_fim and fim and fim != rec.periodo_fim):
            item["situacao"] = "DIVERGENTE"
            item["detalhe"] = f"e-Contador: R$ {d.valor} · {ini:%d/%m/%Y} a {fim:%d/%m/%Y}" if ini and fim else f"e-Contador: R$ {d.valor}"
            continue
        if d.pago:
            item["situacao"] = "JA_PAGO" if d.pago_em == rec.pagamento else "OUTRA_DATA"
            item["detalhe"] = f"pago em {d.pago_em:%d/%m/%Y}" if d.pago_em else "pago"
            continue
        item["situacao"] = "PRONTO"
    return saida


def baixar_honorarios(db: Session, pares: list[tuple[int, str]], usuario: str, cli=None) -> dict:
    """pares = (recebimento_id, documento_id) revisados na tela. Revisa de novo antes de gravar."""
    cli = cli or sessao.cliente(db)
    alvo = dict(pares)
    revisao = {i["rec"].id: i for i in revisar_honorarios(db, list(alvo), cli)}
    por_data: dict[date, list] = defaultdict(list)
    feitos, problemas = 0, []
    for rid, doc_id in alvo.items():
        it = revisao.get(rid)
        if not it:
            continue
        rec = it["rec"]
        if it["situacao"] == "JA_PAGO" and it["doc"] and it["doc"].id == doc_id:
            rec.ec_doc_id, rec.ec_pago_em, rec.ec_baixado_em, rec.ec_erro = doc_id, it["doc"].pago_em, datetime.now(), ""
            feitos += 1
            continue
        if it["situacao"] != "PRONTO" or not it["doc"] or it["doc"].id != doc_id:
            problemas.append(f"{rec.codigo_empresa} título {rec.titulo}: {SITUACOES_REVISAO.get(it['situacao'], ('mudou desde a revisão',))[0]}")
            continue
        por_data[rec.pagamento].append(rec)
    for dia, recs in por_data.items():
        ids = [revisao[r.id]["doc"].id for r in recs]
        try:
            cli.confirmar_pagamento(ids, dia)
        except (ErroWeb, ErroSessao) as e:
            for r in recs:
                r.ec_erro = f"{datetime.now():%d/%m %H:%M} {e} — conferir no e-Contador antes de repetir"
            problemas.append(str(e))
            if isinstance(e, ErroSessao):
                break
            continue
        for r in recs:                     # relê cada cobrança: só conta o que o e-Contador mostra como pago nessa data
            doc_id = revisao[r.id]["doc"].id
            try:
                d = cli.visualizar(doc_id)
            except (ErroWeb, ErroSessao) as e:
                r.ec_erro = f"baixa enviada, mas não consegui reler: {e}"
                problemas.append(f"{r.codigo_empresa} título {r.titulo}: {r.ec_erro}")
                continue
            if d.pago and d.pago_em == dia:
                r.ec_doc_id, r.ec_pago_em, r.ec_baixado_em, r.ec_erro = doc_id, d.pago_em, datetime.now(), ""
                feitos += 1
            else:
                r.ec_erro = "o e-Contador não mostrou o pagamento depois da baixa — conferir lá"
                problemas.append(f"{r.codigo_empresa} título {r.titulo}: {r.ec_erro}")
    db.flush()
    return dict(feitos=feitos, problemas=problemas)


# ---- guias ----------------------------------------------------------------------------------

def titulo_guia(g: models.GuiaRF) -> str:
    from ..guias.servico import TIPOS
    rot = TIPOS[g.tipo][0].replace(" (vencido)", "")
    comp = f"{g.competencia[5:7]}/{g.competencia[:4]}" if len(g.competencia) == 7 else g.competencia
    return f"{rot} {comp} - doc {g.numero_documento or g.id}"


def enviar_guia(db: Session, g: models.GuiaRF, usuario: str, cli=None, mapa: dict | None = None) -> str:
    from ..serpro import servico as sv
    if g.situacao != "ABERTA":
        raise ErroWeb("Só guias em aberto são enviadas ao e-Contador.")
    if g.ec_doc_id and not g.ec_excluida_em:
        raise ErroWeb(f"Esta guia já está no e-Contador (documento {g.ec_doc_id}).")
    arq = sv.arquivo_absoluto(g.arquivo)
    if not arq or not arq.exists():
        raise ErroWeb("O PDF da guia não está no ERP.")
    cli = cli or sessao.cliente(db)
    mapa = mapa if mapa is not None else cli.empresas()
    cod = (g.prestador.codigo_interno or "").lstrip("0")
    emp = mapa.get(cod)
    if not emp:
        raise ErroWeb(f"A empresa {g.prestador.codigo_interno} não aparece na lista do e-Contador.")
    pagar = f" Pagar até {g.vencimento:%d/%m/%Y}." if g.vencimento else ""
    doc_id = cli.enviar(emp, titulo_guia(g), f"Guia emitida pelo escritório (Receita Federal).{pagar}", CATEGORIA_GUIAS,
                        g.vencimento, g.valor, f"{titulo_guia(g)}.pdf".replace("/", "-"), arq.read_bytes())
    g.ec_doc_id, g.ec_enviada_em, g.ec_erro, g.ec_excluida_em = doc_id, datetime.now(), "", None
    # guias anteriores do mesmo período, substituídas por esta, saem do e-Contador
    velhas = db.scalars(select(models.GuiaRF).where(
        models.GuiaRF.prestador_id == g.prestador_id, models.GuiaRF.tipo == g.tipo, models.GuiaRF.competencia == g.competencia,
        models.GuiaRF.situacao == "SUBSTITUIDA", models.GuiaRF.ec_doc_id != "", models.GuiaRF.ec_excluida_em.is_(None))).all()
    for v in velhas:
        excluir_guia(db, v, f"Substituída pela guia recalculada {doc_id} (ERP)", cli)
    db.flush()
    return doc_id


def excluir_guia(db: Session, g: models.GuiaRF, motivo: str, cli=None) -> None:
    cli = cli or sessao.cliente(db)
    d = cli.visualizar(g.ec_doc_id)
    if d.pago:
        g.ec_erro = "não excluída: consta como paga no e-Contador"
        return
    cli.excluir([g.ec_doc_id], motivo)
    try:
        cli.visualizar(g.ec_doc_id)
        g.ec_erro = "pedido de exclusão enviado, mas o documento continua no e-Contador — conferir"
    except ErroWeb:
        g.ec_excluida_em, g.ec_erro = datetime.now(), ""


def baixar_guia(db: Session, g: models.GuiaRF, cli=None) -> bool:
    if g.situacao != "PAGA" or not g.ec_doc_id or g.ec_baixada_em or g.ec_excluida_em or not g.pago_em:
        return False
    cli = cli or sessao.cliente(db)
    d = cli.visualizar(g.ec_doc_id)
    if not d.pago:
        cli.confirmar_pagamento([g.ec_doc_id], g.pago_em)
        d = cli.visualizar(g.ec_doc_id)
    if d.pago:
        g.ec_baixada_em, g.ec_erro = datetime.now(), ""
        return True
    g.ec_erro = "baixa enviada, mas o e-Contador não mostrou como pago — conferir"
    return False


def baixar_guias_pagas(db: Session) -> dict:
    """Depois da conciliação: dá baixa no e-Contador das guias pagas que o ERP enviou."""
    pend = db.scalars(select(models.GuiaRF).where(models.GuiaRF.situacao == "PAGA", models.GuiaRF.ec_doc_id != "",
                                                  models.GuiaRF.ec_baixada_em.is_(None), models.GuiaRF.ec_excluida_em.is_(None))).all()
    if not pend:
        return dict(baixadas=0, problemas=[])
    feitas, prob = 0, []
    try:
        cli = sessao.cliente(db)
    except ErroSessao as e:
        return dict(baixadas=0, problemas=[str(e)])
    for g in pend:
        try:
            feitas += baixar_guia(db, g, cli)
        except ErroSessao as e:
            prob.append(str(e))
            break
        except ErroWeb as e:
            g.ec_erro = str(e)
            prob.append(f"{g.prestador.razao_social}: {e}")
    db.flush()
    return dict(baixadas=feitas, problemas=prob)
