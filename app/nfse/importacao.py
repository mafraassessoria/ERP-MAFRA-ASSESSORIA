"""Importação de NFS-e pelo ADN (Ambiente de Dados Nacional) — a "caixa postal" de cada CNPJ.

Traz, para cada empresa:
  • as notas que ela EMITIU (inclusive pelo portal ou por outro sistema) → papel PRESTADO;
  • as notas que ela RECEBEU como tomadora → papel TOMADO (úteis para a escrita fiscal);
  • os eventos (cancelamentos), que atualizam a situação das notas já guardadas.

A leitura é incremental pelo NSU: cada execução continua de onde a anterior parou.
O certificado usado é o da própria empresa ou, se marcado "importar por procuração",
o certificado do escritório (exige procuração eletrônica do cliente para o escritório).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from lxml import etree
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import XML_DIR
from ..configuracoes import copiar_para_pasta_extra, ler_config
from ..documentos import limpar
from .emissao import AMB_LIB, ErroEmissao, _certificado_temporario, dono_certificado

log = logging.getLogger("emissor.importacao")

# Eventos que cancelam a nota: cancelamento, cancelamento por substituição,
# cancelamento deferido em análise fiscal.
EVENTOS_CANCELAMENTO = ("e101101", "e105102", "e105104")
MAX_RODADAS = 5  # até ~5.000 documentos por execução (a próxima continua)


@dataclass
class ResultadoImportacao:
    ok: bool
    mensagem: str
    novas_prestadas: int = 0
    novas_tomadas: int = 0
    canceladas: int = 0
    ja_existentes: int = 0
    tomadores_novos: int = 0
    erros: list[str] = field(default_factory=list)


def _txt(el, caminho: str) -> str:
    achado = el.find(caminho) if el is not None else None
    return (achado.text or "").strip() if achado is not None and achado.text else ""


def _doc(el) -> str:
    if el is None:
        return ""
    for tag in ("CNPJ", "CPF", "NIF"):
        v = _txt(el, f"{{*}}{tag}")
        if v:
            return limpar(v)
    return ""


def dados_da_nfse(xml: str) -> dict:
    """Extrai o essencial do XML da NFS-e (o que a Sefin/ADN devolve)."""
    raiz = etree.fromstring(xml.encode("utf-8"))
    inf = raiz.find(".//{*}infNFSe")
    if inf is None:
        raise ValueError("XML sem infNFSe")
    chave = (inf.get("Id") or "")[3:]
    inf_dps = inf.find(".//{*}infDPS")
    prest = inf_dps.find("{*}prest") if inf_dps is not None else None
    toma = inf_dps.find("{*}toma") if inf_dps is not None else None
    emit = inf.find("{*}emit")
    d = {
        "chave": chave,
        "numero": _txt(inf, "{*}nNFSe"),
        "dh_proc": _txt(inf, "{*}dhProc"),
        "competencia": _txt(inf_dps, "{*}dCompet"),
        "tp_amb": _txt(inf_dps, "{*}tpAmb") or _txt(inf, "{*}ambGer"),
        "prestador_doc": _doc(prest) or _doc(emit),
        "prestador_nome": _txt(emit, "{*}xNome") or _txt(prest, "{*}xNome"),
        "tomador_doc": _doc(toma),
        "tomador_nome": _txt(toma, "{*}xNome"),
        "descricao": _txt(inf_dps, ".//{*}cServ/{*}xDescServ"),
        "ctribnac": _txt(inf_dps, ".//{*}cServ/{*}cTribNac"),
        "ctribmun": _txt(inf_dps, ".//{*}cServ/{*}cTribMun"),
        "vserv": _txt(inf_dps, ".//{*}vServPrest/{*}vServ"),
        "viss": _txt(inf.find("{*}valores"), "{*}vISSQN"),
        "id_dps": inf_dps.get("Id") if inf_dps is not None else None,
        "tp_ret": _txt(inf_dps, ".//{*}tribMun/{*}tpRetISSQN"),
    }
    return d


def _data(txt: str) -> date | None:
    try:
        return date.fromisoformat(txt[:10]) if txt else None
    except ValueError:
        return None


def _datahora(txt: str) -> datetime | None:
    try:
        return datetime.fromisoformat(txt).replace(tzinfo=None) if txt else None
    except ValueError:
        return None


def _pasta(prestador: models.Prestador, competencia: date | None, papel: str):
    ref = competencia or date.today()
    sub = "recebidas" if papel == "TOMADO" else ""
    pasta = XML_DIR / prestador.cnpj / f"{ref:%Y}" / f"{ref:%m}"
    if sub:
        pasta = pasta / sub
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta


def _gravar_nota(db: Session, p: models.Prestador, xml: str, nsu: int, res: ResultadoImportacao) -> None:
    d = dados_da_nfse(xml)
    if not d["chave"]:
        res.erros.append(f"NSU {nsu}: NFS-e sem chave")
        return
    existente = db.scalar(select(models.NotaFiscal).where(
        models.NotaFiscal.prestador_id == p.id, models.NotaFiscal.chave_acesso == d["chave"]))
    papel = "PRESTADO" if d["prestador_doc"] == p.cnpj else "TOMADO"
    competencia = _data(d["competencia"])
    ambiente = "PRODUCAO" if d["tp_amb"] == "1" else "HOMOLOGACAO"
    prefixo = "HOMOLOG_" if ambiente == "HOMOLOGACAO" else ""

    if existente:
        res.ja_existentes += 1
        if not existente.caminho_xml:  # ex.: nota emitida aqui cujo XML não veio na hora
            caminho = _pasta(p, competencia, papel) / f"{prefixo}NFSe_{d['chave']}.xml"
            caminho.write_text(xml, encoding="utf-8")
            existente.caminho_xml = str(caminho.relative_to(XML_DIR))
        if existente.nsu is None:
            existente.nsu = nsu
        return

    caminho = _pasta(p, competencia, papel) / f"{prefixo}NFSe_{d['chave']}.xml"
    caminho.write_text(xml, encoding="utf-8")
    copiar_para_pasta_extra(db, p.cnpj, caminho)
    n = models.NotaFiscal(
        prestador_id=p.id, origem="IMPORTADA", papel=papel, situacao="AUTORIZADA", ambiente=ambiente,
        chave_acesso=d["chave"], numero_nfse=d["numero"] or None, nsu=nsu, id_dps=d["id_dps"],
        competencia=competencia, data_emissao=_datahora(d["dh_proc"]),
        tomador_documento=d["tomador_doc"], tomador_nome=d["tomador_nome"] or ("" if d["tomador_doc"] else "CONSUMIDOR NÃO IDENTIFICADO"),
        emitente_documento=d["prestador_doc"], emitente_nome=d["prestador_nome"],
        descricao_servico=d["descricao"], codigo_tributacao_nacional=d["ctribnac"][:6],
        codigo_tributacao_municipal=d["ctribmun"][:3],
        valor_servicos=Decimal(d["vserv"] or "0"), valor_iss=Decimal(d["viss"] or "0"),
        tipo_retencao_iss=int(d["tp_ret"]) if d["tp_ret"].isdigit() else 1,
        caminho_xml=str(caminho.relative_to(XML_DIR)), emitida_por="", criada_por="importação ADN",
    )
    db.add(n)
    completar_nota_importada(db, n, xml, res)
    if papel == "PRESTADO":
        res.novas_prestadas += 1
    else:
        res.novas_tomadas += 1


def completar_nota_importada(db: Session, n: models.NotaFiscal, xml: str, res: "ResultadoImportacao | None" = None) -> None:
    """Retenções/alíquota/códigos do XML e, nas notas prestadas, cadastro automático do tomador e da obra."""
    from .cadastro_auto import aplicar_valores, obra_da_nota, tomador_da_nota
    from .nfse_xml import extrair
    d = extrair(xml)
    if not d:
        return
    aplicar_valores(n, d)
    if n.papel == "PRESTADO":
        novo = d.get("toma_doc") and db.scalar(select(models.Tomador).where(
            models.Tomador.prestador_id == n.prestador_id, models.Tomador.documento == d["toma_doc"])) is None
        t = tomador_da_nota(db, n.prestador_id, d)
        if t is not None:
            n.tomador_id = t.id
            if novo and res is not None:
                res.tomadores_novos += 1
        o = obra_da_nota(db, n.prestador_id, t, d)
        if o is not None:
            n.obra_id = o.id
        # Os códigos usados pela empresa entram na lista dela (aparecem na emissão)
        from . import tabelas
        if d.get("ctn"):
            tabelas.gravar_ctribnac(db, d["ctn"], d.get("xTribNac") or "")
            tabelas.vincular_codigo(db, n.prestador_id, "CTRIBNAC", d["ctn"])
        if d.get("nbs"):
            tabelas.gravar_nbs(db, d["nbs"], "")
            tabelas.vincular_codigo(db, n.prestador_id, "NBS", d["nbs"])


def _gravar_evento(db: Session, p: models.Prestador, doc, res: ResultadoImportacao) -> None:
    xml = doc.xml or ""
    chave = doc.chave_acesso
    if not chave:
        m = etree.fromstring(xml.encode("utf-8")).find(".//{*}chNFSe") if xml else None
        chave = (m.text or "").strip() if m is not None else None
    if not chave:
        return
    cancela = any(cod in xml for cod in EVENTOS_CANCELAMENTO) or \
        "CANCELAMENTO" in (doc.tipo_evento or "").upper().replace("Ç", "C")
    nota = db.scalar(select(models.NotaFiscal).where(
        models.NotaFiscal.prestador_id == p.id, models.NotaFiscal.chave_acesso == chave))
    if nota is None:
        return  # evento de nota que não está conosco (ex.: anterior ao início da importação)
    if xml:
        pasta = (XML_DIR / nota.caminho_xml).parent if nota.caminho_xml else _pasta(p, nota.competencia, nota.papel)
        caminho = pasta / f"EVENTO_{doc.nsu}_{chave}.xml"
        caminho.write_text(xml, encoding="utf-8")
        if cancela and not nota.caminho_evento_xml:
            nota.caminho_evento_xml = str(caminho.relative_to(XML_DIR))
    if cancela and nota.situacao != "CANCELADA":
        nota.situacao = "CANCELADA"
        nota.cancelada_em = _datahora(doc.data_hora or "") or datetime.now()
        nota.cancelada_por = nota.cancelada_por or "ADN"
        nota.motivo_cancelamento = nota.motivo_cancelamento or f"Evento registrado no ADN ({doc.tipo_evento or 'cancelamento'})"
        if nota.caminho_pdf:
            from .emissao import _refazer_pdf
            _refazer_pdf(nota)
        res.canceladas += 1


def certificado_para(db: Session, p: models.Prestador) -> models.Prestador:
    """De quem é o certificado usado na consulta: da própria empresa ou do escritório."""
    return dono_certificado(db, p)


def importar(db: Session, p: models.Prestador, usuario: str = "") -> ResultadoImportacao:
    try:
        from pynfsenacional import dfe, sefin
    except ImportError:
        return ResultadoImportacao(False, "Componente da NFS-e Nacional não instalado. Execute o "
                                          "ATUALIZAR_SISTEMA.bat no servidor.")

    ctrl = db.scalar(select(models.ControleNSU).where(models.ControleNSU.prestador_id == p.id))
    if ctrl is None:
        ctrl = models.ControleNSU(prestador_id=p.id, ambiente=p.ambiente, ultimo_nsu=0)
        db.add(ctrl)
    if ctrl.ambiente != p.ambiente:  # trocou de ambiente: a caixa postal é outra
        ctrl.ambiente, ctrl.ultimo_nsu = p.ambiente, 0

    res = ResultadoImportacao(ok=True, mensagem="")
    try:
        dono_cert = certificado_para(db, p)
        with _certificado_temporario(dono_cert) as (pfx, senha):
            cliente = sefin.SefinClient(ambiente=AMB_LIB[p.ambiente], cert_pfx_path=pfx, senha=senha,
                                        codigo_municipio=p.codigo_municipio_ibge or None)
            for _ in range(MAX_RODADAS):
                r = dfe.sincronizar(cliente, p.cnpj, ctrl.ultimo_nsu)
                for doc in sorted(r.documentos, key=lambda x: x.nsu):
                    try:
                        if doc.evento:
                            _gravar_evento(db, p, doc, res)
                        elif doc.xml:
                            _gravar_nota(db, p, doc.xml, doc.nsu, res)
                    except Exception as e:  # noqa: BLE001 — um documento ruim não para o lote
                        log.exception("documento NSU %s", doc.nsu)
                        res.erros.append(f"NSU {doc.nsu}: {e}")
                    ctrl.ultimo_nsu = max(ctrl.ultimo_nsu, doc.nsu)
                    db.flush()
                ctrl.ultimo_nsu = max(ctrl.ultimo_nsu, r.ultimo_nsu)
                db.commit()
                if r.erro:
                    res.ok = False
                    res.erros.append(r.erro)
                    break
                if not r.tem_mais:
                    break
    except ErroEmissao as e:
        res.ok, res.mensagem = False, str(e)
    except Exception as e:  # noqa: BLE001
        log.exception("importação %s", p.cnpj)
        db.rollback()
        res.ok, res.mensagem = False, f"Falha na consulta ao ADN ({type(e).__name__}: {e})"

    if not res.mensagem:
        partes = [f"{res.novas_prestadas} nota(s) emitida(s) nova(s)", f"{res.novas_tomadas} recebida(s) nova(s)",
                  f"{res.canceladas} cancelamento(s)", f"{res.tomadores_novos} tomador(es) cadastrado(s)"]
        res.mensagem = "Importação: " + ", ".join(partes) + "."
        if res.erros:
            res.ok = False
            res.mensagem += " Problemas: " + " | ".join(res.erros[:5])
    ctrl.ultima_consulta = datetime.now()
    ctrl.ultimo_resultado = res.mensagem[:2000]
    db.commit()
    return res


def importar_todos(db: Session, usuario: str = "agendador", pausa_s: float = 2.0) -> list[tuple[models.Prestador, ResultadoImportacao]]:
    """Importa de todas as empresas ativas marcadas para importação automática."""
    saida = []
    prestadores = db.scalars(select(models.Prestador).where(
        models.Prestador.ativo.is_(True), models.Prestador.usa_nfse.is_(True),
        models.Prestador.importar_automaticamente.is_(True))
        .order_by(*models.ordem_prestador_sql())).all()
    for i, p in enumerate(prestadores):
        from .emissao import descricao_certificado
        if descricao_certificado(db, p)[1]:
            continue  # sem certificado utilizável (nem da empresa, nem do escritório)
        if i:
            time.sleep(pausa_s)  # o ADN limita a taxa de consultas
        saida.append((p, importar(db, p, usuario)))
    return saida
