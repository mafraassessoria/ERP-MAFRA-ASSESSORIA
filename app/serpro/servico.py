"""Execução das consultas do Integra Contador: configuração, custo, repetição, PDFs e histórico."""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models
from ..config import DATA_DIR
from ..configuracoes import gravar_config, ler_config
from ..seguranca import criptografar, descriptografar
from .catalogo import POR_CODIGO, PRECOS_PADRAO, Servico
from .cliente import ClienteIntegra, ErroSerpro, Resposta

log = logging.getLogger("emissor.serpro")
PASTA = DATA_DIR / "receita"
MODOS = {"desligado": "Desligado", "demonstracao": "Demonstração (ambiente de testes do Serpro, dados fictícios, sem custo)",
         "producao": "Produção (contrato com o Serpro, cobra por chamada)"}
ESPERA_MAX_SITFIS = 60  # segundos aguardando o relatório ficar pronto


class ErroCorte(Exception):
    """Pedido de dado anterior à data de corte configurada (nada é chamado nem cobrado)."""


class ErroConsulta(Exception):
    """Pedido recusado antes de chamar o Serpro (nada foi cobrado)."""


# ---- Configuração ------------------------------------------------------------------

def modo(db: Session) -> str:
    m = ler_config(db, "serpro_modo") or "desligado"
    return m if m in MODOS else "desligado"


def precos(db: Session) -> dict[str, Decimal]:
    out = {}
    for tipo, padrao in PRECOS_PADRAO.items():
        v = ler_config(db, f"serpro_preco_{tipo}") or padrao
        try:
            out[tipo] = Decimal(v.replace(".", "").replace(",", ".")) if "," in v else Decimal(v)
        except InvalidOperation:
            out[tipo] = Decimal(padrao.replace(",", "."))
    return out


def limite_mensal(db: Session) -> Decimal:
    v = ler_config(db, "serpro_limite_mensal") or "200,00"
    try:
        return Decimal(v.replace(".", "").replace(",", "."))
    except InvalidOperation:
        return Decimal("200")


def gravar_chaves(db: Session, ck: str, cs: str) -> None:
    if ck:
        gravar_config(db, "serpro_ck_cripto", criptografar(ck.encode()).decode())
    if cs:
        gravar_config(db, "serpro_cs_cripto", criptografar(cs.encode()).decode())


def _ler_cripto(db: Session, chave: str) -> str:
    v = ler_config(db, chave)
    return descriptografar(v.encode()).decode() if v else ""


def tem_chaves(db: Session) -> bool:
    return bool(ler_config(db, "serpro_ck_cripto") and ler_config(db, "serpro_cs_cripto"))


def escritorio(db: Session) -> models.Prestador | None:
    pid = ler_config(db, "prestador_procurador_id")
    return db.get(models.Prestador, int(pid)) if pid and pid.isdigit() else None


def cliente(db: Session) -> ClienteIntegra:
    try:
        return _cliente(db)
    except ErroSerpro as e:
        raise ErroConsulta(str(e)) from e


def _cliente(db: Session) -> ClienteIntegra:
    m = modo(db)
    if m == "desligado":
        raise ErroConsulta("O módulo Receita Federal está desligado. Ative em Configurações → Receita Federal (Integra Contador).")
    esc = escritorio(db)
    if m == "demonstracao":
        return ClienteIntegra(modo=m, contratante=esc.cnpj if esc else "")
    if esc is None:
        raise ErroConsulta("Defina em Configurações qual empresa é o escritório (contratante e procurador).")
    cert = esc.certificado_ativo
    if cert is None or cert.validade_fim < datetime.now():
        raise ErroConsulta("O escritório precisa de certificado A1 (e-CNPJ) válido para autenticar no Serpro.")
    from ..certificados import abrir_certificado
    pfx, senha = abrir_certificado(cert)
    return ClienteIntegra(modo=m, contratante=esc.cnpj, consumer_key=_ler_cripto(db, "serpro_ck_cripto"),
                          consumer_secret=_ler_cripto(db, "serpro_cs_cripto"), pfx=pfx, senha_pfx=senha)


# ---- Custo ---------------------------------------------------------------------------

def chamadas_previstas(s: Servico) -> list[str]:
    return ["Apoiar", "Emitir"] if s.codigo == "sitfis" else [s.tipo]


def custo_previsto(db: Session, s: Servico) -> Decimal:
    p = precos(db)
    return sum((p.get(t, Decimal("0")) for t in chamadas_previstas(s)), Decimal("0"))


def gasto_mes(db: Session, ref: date | None = None) -> Decimal:
    ref = ref or date.today()
    inicio = datetime(ref.year, ref.month, 1)
    v = db.scalar(select(func.coalesce(func.sum(models.ConsultaReceita.custo), 0)).where(
        models.ConsultaReceita.data_hora >= inicio, models.ConsultaReceita.modo == "producao"))
    from .apis import gasto_mes as gasto_apis   # Consulta CPF e Consulta CND entram no mesmo limite
    return Decimal(str(v or 0)) + gasto_apis(db, ref)


def chamadas_mes(db: Session, ref: date | None = None) -> int:
    ref = ref or date.today()
    inicio = datetime(ref.year, ref.month, 1)
    return int(db.scalar(select(func.coalesce(func.sum(models.ConsultaReceita.chamadas), 0)).where(
        models.ConsultaReceita.data_hora >= inicio, models.ConsultaReceita.modo == "producao")) or 0)


# ---- PDFs ----------------------------------------------------------------------------

def _parece_pdf(v) -> bool:
    return isinstance(v, str) and len(v) > 200 and v.lstrip().startswith("JVBER")


def separar_pdfs(dados, achados: list[tuple[str, bytes]], caminho: str = "") -> object:
    """Tira os PDFs (base64) dos dados e devolve os dados com uma marca no lugar."""
    if isinstance(dados, dict):
        return {k: separar_pdfs(v, achados, f"{caminho}.{k}" if caminho else k) for k, v in dados.items()}
    if isinstance(dados, list):
        return [separar_pdfs(v, achados, f"{caminho}[{i}]") for i, v in enumerate(dados)]
    if _parece_pdf(dados):
        try:
            achados.append((caminho, base64.b64decode(dados)))
            return f"[PDF {len(achados)}]"
        except (ValueError, TypeError):
            return dados
    return dados


def _gravar_pdfs(p: models.Prestador, s: Servico, rotulo: str, pdfs: list[tuple[str, bytes]]) -> list[str]:
    if not pdfs:
        return []
    pasta = PASTA / p.cnpj / date.today().strftime("%Y-%m")
    pasta.mkdir(parents=True, exist_ok=True)
    carimbo = datetime.now().strftime("%Y%m%d_%H%M%S")
    caminhos = []
    for i, (_c, conteudo) in enumerate(pdfs, start=1):
        sufixo = f"_{i}" if len(pdfs) > 1 else ""
        nome = re.sub(r"[^A-Za-z0-9_.-]", "", f"{s.codigo}_{rotulo}_{carimbo}{sufixo}.pdf")
        arq = pasta / nome
        arq.write_bytes(conteudo)
        caminhos.append(str(arq.relative_to(DATA_DIR)))
    return caminhos


# ---- Execução --------------------------------------------------------------------------

def rotulo_parametros(valores: dict) -> str:
    return "_".join(re.sub(r"\W", "", str(v)) for k, v in sorted(valores.items()) if v and not k.startswith("_"))[:60]


def ultima_igual(db: Session, p: models.Prestador, s: Servico, valores: dict) -> models.ConsultaReceita | None:
    chave = f"{s.codigo}|{rotulo_parametros(valores)}"
    hoje = datetime.combine(date.today(), datetime.min.time())
    return db.scalar(select(models.ConsultaReceita).where(
        models.ConsultaReceita.prestador_id == p.id, models.ConsultaReceita.chave_repeticao == chave,
        models.ConsultaReceita.ok.is_(True), models.ConsultaReceita.data_hora >= hoje,
        models.ConsultaReceita.modo == modo(db)).order_by(models.ConsultaReceita.id.desc()).limit(1))


CORTE_PADRAO = "2026-11-01"
SERVICOS_PARCELA = ("parcsn_das", "parcmei_das", "parcsnesp_das", "pertsn_das", "relpsn_das",
                    "parcmeiesp_das", "pertmei_das", "relpmei_das")


def data_corte(db: Session) -> date | None:
    """Nenhum dado anterior a esta data é consultado no Serpro (pedido do escritório em 10/11/2026).
    Regra "pelo movimento": a apuração do mês anterior ao corte (que vence depois dele) é permitida."""
    reg = db.get(models.Configuracao, "serpro_data_corte")
    v = (reg.valor or "") if reg is not None else CORTE_PADRAO
    try:
        return date.fromisoformat(v.strip()) if v.strip() else None
    except ValueError:
        return date.fromisoformat(CORTE_PADRAO)


def aplicar_corte(db: Session, s, valores: dict) -> None:
    corte = data_corte(db)
    if not corte:
        return
    msg = f"Configurado para não consultar dados anteriores a {corte:%d/%m/%Y} (Configurações → Receita Federal)."
    mes_corte = corte.strftime("%Y-%m")
    mes_ant = (corte.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    for c in s.campos:
        v = valores.get(c.nome) or ""
        if not v:
            continue
        if c.tipo == "periodo":
            minimo = mes_corte if s.codigo in SERVICOS_PARCELA else mes_ant
            if v[:7] < minimo:
                raise ErroCorte(msg)
        elif c.tipo == "ano":
            if v[:4].isdigit() and int(v[:4]) < int(mes_ant[:4]):
                raise ErroCorte(msg)
        elif c.tipo == "data" and c.nome in ("de", "ate"):
            try:
                d = date.fromisoformat(v[:10])
            except ValueError:
                continue
            if d < corte:
                if c.nome == "ate":
                    raise ErroCorte(msg)
                valores[c.nome] = corte.isoformat()     # período de pagamentos começa no corte


def executar(db: Session, p: models.Prestador, codigo: str, valores: dict, usuario: str, *,
             lote: str = "", cli: ClienteIntegra | None = None, espera=time.sleep) -> models.ConsultaReceita:
    s = POR_CODIGO.get(codigo)
    if s is None:
        raise ErroConsulta("Serviço desconhecido.")
    valores = {c.nome: (valores.get(c.nome) or "").strip() for c in s.campos}
    try:
        aplicar_corte(db, s, valores)
    except ErroCorte as e:
        raise ErroConsulta(str(e)) from e
    try:
        dados = s.montar(valores)
    except (ValueError, TypeError) as e:
        raise ErroConsulta(str(e)) from e
    m = modo(db)
    if m == "producao":
        previsto = custo_previsto(db, s)
        if gasto_mes(db) + previsto > limite_mensal(db):
            raise ErroConsulta(f"Limite de gasto mensal com o Serpro atingido (R$ {limite_mensal(db):.2f}). "
                               "Aumente o limite em Configurações para continuar.")
    cli = cli or cliente(db)
    if s.codigo == "procuracoes":
        esc = escritorio(db)
        outorgado = (esc.cnpj if esc else cli.contratante) or "00000000000000"
        dados = {"outorgante": p.cnpj, "tipoOutorgante": "2" if len(p.cnpj) == 14 else "1",
                 "outorgado": outorgado, "tipoOutorgado": "2"}
    c = models.ConsultaReceita(prestador_id=p.id, servico=s.codigo, id_servico=s.id_servico, tipo=s.tipo,
                               parametros=json.dumps(valores, ensure_ascii=False), modo=m, usuario=usuario,
                               chave_repeticao=f"{s.codigo}|{rotulo_parametros(valores)}", lote=lote)
    chamadas: list[str] = []
    try:
        if s.codigo == "sitfis":
            r = _sitfis(cli, p, s, chamadas, espera)
        else:
            r = cli.chamar(s.tipo, p.cnpj, s.id_sistema, s.id_servico, s.versao_para(m), dados)
            chamadas.append(s.tipo)
    except ErroSerpro as e:
        c.ok, c.http, c.mensagens = False, 0, str(e)
        c.chamadas = len(chamadas)
        c.custo = _custo(db, m, chamadas)
        db.add(c)
        db.flush()
        return c
    c.http = r.http
    c.ok = r.ok
    c.mensagens = r.texto_mensagens or ("Relatório ainda em processamento na Receita. Tente de novo em alguns minutos."
                                        if r.em_processamento else "")
    pdfs: list[tuple[str, bytes]] = []
    limpos = separar_pdfs(r.dados, pdfs) if r.dados is not None else None
    c.dados = json.dumps(limpos, ensure_ascii=False) if limpos is not None else ""
    c.arquivos = "\n".join(_gravar_pdfs(p, s, rotulo_parametros(valores) or "consulta", pdfs))
    c.chamadas = len(chamadas)
    c.custo = _custo(db, m, chamadas)
    db.add(c)
    db.flush()
    return c


def _custo(db: Session, m: str, chamadas: list[str]) -> Decimal:
    if m != "producao":
        return Decimal("0")
    p = precos(db)
    return sum((p.get(t, Decimal("0")) for t in chamadas), Decimal("0"))


def _sitfis(cli: ClienteIntegra, p: models.Prestador, s: Servico, chamadas: list[str], espera) -> Resposta:
    """Situação fiscal: Apoiar (protocolo) → aguarda → Emitir (relatório)."""
    versao = s.versao_para(cli.modo)
    r1 = cli.chamar("Apoiar", p.cnpj, "SITFIS", "SOLICITARPROTOCOLO91", versao, "")
    chamadas.append("Apoiar")
    if not r1.ok:
        return r1
    d = r1.dados if isinstance(r1.dados, dict) else {}
    protocolo = d.get("protocoloRelatorio")
    if not protocolo:
        return r1
    aguardar = min(int(d.get("tempoEspera") or r1.tempo_espera_ms or 0) / 1000, ESPERA_MAX_SITFIS)
    total = 0.0
    for _tentativa in range(4):
        if aguardar > 0:
            espera(aguardar)
            total += aguardar
        r2 = cli.chamar("Emitir", p.cnpj, "SITFIS", "RELATORIOSITFIS92", versao, {"protocoloRelatorio": protocolo})
        chamadas.append("Emitir")
        if not r2.em_processamento or total >= ESPERA_MAX_SITFIS:
            return r2
        aguardar = min(max(r2.tempo_espera_ms / 1000, 2), ESPERA_MAX_SITFIS - total)
    return r2


def arquivo_absoluto(rel: str) -> Path | None:
    arq = (DATA_DIR / rel).resolve()
    try:
        arq.relative_to(PASTA.resolve())
    except ValueError:
        return None
    return arq if arq.exists() else None


# ---- Limpeza do ambiente de demonstração -------------------------------------------------------

def limpar_demonstracao(db: Session) -> dict:
    """Apaga tudo o que veio do ambiente de demonstração (dados fictícios): consultas do Integra Contador e das
    APIs avulsas, resultados do Monitor, pagamentos, guias não enviadas e certidões ligados a elas, e os PDFs."""
    from .. import models as m
    demo = db.scalars(select(m.ConsultaReceita).where(m.ConsultaReceita.modo != "producao")).all()
    ids = {c.id for c in demo}
    textos = [c.dados or "" for c in demo]
    cont = dict(consultas=0, monitor=0, pagamentos=0, guias=0, guias_mantidas=0, apis=0, certidoes=0)
    # Monitor: resultados ligados a consultas de demonstração
    for it in db.scalars(select(m.MonitorItem).where(m.MonitorItem.consulta_id.in_(ids))).all() if ids else []:
        db.delete(it)
        cont["monitor"] += 1
    # Pagamentos cujo número de documento só existe nas respostas fictícias
    if textos:
        for pg in db.scalars(select(m.PagamentoRF)).all():
            if pg.comprovante_consulta_id in ids or (pg.numero_documento and any(pg.numero_documento in t for t in textos)):
                db.delete(pg)
                cont["pagamentos"] += 1
    # Guias geradas por consultas de demonstração (as já enviadas ao e-Contador ficam, para conferência)
    for g in db.scalars(select(m.GuiaRF).where(m.GuiaRF.consulta_id.in_(ids))).all() if ids else []:
        if g.ec_doc_id:
            g.consulta_id = None
            cont["guias_mantidas"] += 1
            continue
        _apagar_arquivo(g.arquivo)
        db.delete(g)
        cont["guias"] += 1
    for c in demo:
        for rel in c.lista_arquivos:
            _apagar_arquivo(rel)
        db.delete(c)
        cont["consultas"] += 1
    # APIs avulsas (Consulta CPF / CND) em demonstração e certidões federais emitidas por elas
    apis_demo = db.scalars(select(m.ConsultaAPI).where(m.ConsultaAPI.modo != "producao")).all()
    textos_api = [re.sub(r"[^0-9A-Za-z]", "", a.dados or "") for a in apis_demo if a.produto == "CND"]
    if textos_api:
        for cert in db.scalars(select(m.Certidao).where(m.Certidao.pack_id.like("RFB-%"))).all():
            cod = (cert.pack_id or "")[4:]
            if cod and any(cod in t for t in textos_api):
                _apagar_arquivo(cert.arquivo)
                db.delete(cert)
                cont["certidoes"] += 1
    for a in apis_demo:
        db.delete(a)
        cont["apis"] += 1
    db.flush()
    return cont


def _apagar_arquivo(rel: str) -> None:
    if not rel:
        return
    try:
        arq = (DATA_DIR / rel).resolve()
        if DATA_DIR.resolve() in arq.parents and arq.is_file():
            arq.unlink()
    except OSError:
        pass


def limpar_demonstracao_e_registrar(db: Session, usuario: str = "sistema") -> dict:
    cont = limpar_demonstracao(db)
    if any(cont.values()):
        db.add(models.Auditoria(usuario_login=usuario, acao="LIMPEZA_DEMONSTRACAO", entidade="receita",
                                detalhes=", ".join(f"{k}={v}" for k, v in cont.items()), data_hora=datetime.now()))
    db.commit()
    return cont
