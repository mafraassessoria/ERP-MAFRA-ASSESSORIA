"""Coleta das certidões no Pack CND (API do e-Contador) e entrega ao Bot Arquivador.

Fluxo:
1. consulta as certidões de cada empresa com os módulos e-Contador e Certidões ligados;
2. grava/atualiza o registro e baixa o PDF das certidões válidas que mudaram (para dados/certidoes/<CNPJ>/);
3. para cada empresa e tipo, a certidão mais recente, válida e negativa é entregue na pasta de entrada do
   Bot Arquivador (grava .pending, confere o hash e renomeia). O arquivador renomeia e guarda na pasta
   Certidões da empresa no Drive.

Nunca entrega uma certidão se já houver, para a mesma empresa e tipo, outra igual ou mais nova entregue pelo
sistema ou já presente na pasta Certidões do Drive (quando a pasta estiver acessível).
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import DATA_DIR
from ..configuracoes import gravar_config, ler_config
from ..econtador.cliente import ClienteEContador, ErroAPI
from . import tipos as T

log = logging.getLogger(__name__)
PASTA = DATA_DIR / "certidoes"
ENTRADA_PADRAO = r"C:\Monitor de Arquivos"
DIAS_AVISO = 15
DIAS_SEMANA = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]

ESTADOS = {
    "VALIDA": ("Válida", "ok"), "VENCE_LOGO": ("Vence em breve", "aviso"), "VENCIDA": ("Vencida", "erro"),
    "POSITIVA": ("Positiva", "erro"), "IMPEDIMENTO": ("Com impedimento", "erro"), "NAO_EMITIDA": ("Não emitida", "aviso"),
    "AGUARDANDO": ("Aguardando órgão", "aviso"),
    "INDISPONIVEL": ("Sem arquivo no Pack", "aviso"),
    "SEM_VALIDADE": ("Sem validade — conferir", "aviso"), "SUBSTITUIDA": ("Substituída por outra mais nova", ""),
}

# Próximo passo sugerido para cada estado (coluna "O que fazer"); por tipo quando faz diferença.
PROBLEMAS = ("POSITIVA", "IMPEDIMENTO")   # filtro "Positiva ou com impedimento"


def o_que_fazer(estado_: str, tipo: str = "") -> str:
    if estado_ == "NAO_EMITIDA":
        if tipo.startswith("FALENCIA"):
            return "Emitir de novo no Pack ou no site do TJ"
        return "Emitir de novo no Pack ou no site do órgão"
    if estado_ == "AGUARDANDO":
        return "Aguardar o órgão emissor; o Pack atualiza na próxima consulta"
    if estado_ == "IMPEDIMENTO":
        return "Ver a pendência no FGTS (Caixa/FGTS Digital) e regularizar" if tipo == "FGTS" else "Ver o impedimento no órgão e regularizar"
    if estado_ == "POSITIVA":
        if tipo == "FEDERAL":
            return "Abrir o PDF; tratar os débitos (ver Situação fiscal)"
        return "Abrir o PDF e tratar o débito apontado"
    if estado_ == "VENCIDA":
        return "Renovar a certidão"
    if estado_ == "VENCE_LOGO":
        return "Renovar antes do vencimento"
    if estado_ == "INDISPONIVEL":
        return "Aguardar o Pack baixar ou emitir no site"
    if estado_ == "SEM_VALIDADE":
        return "Abrir o PDF e conferir a validade"
    return ""


_execucao = {"rodando": False, "inicio": None, "fim": None, "feitas": 0, "total": 0, "erros": [], "resumo": "", "por": ""}
_trava = threading.Lock()


# ---- configuração ------------------------------------------------------------------------

def entrada(db: Session) -> str:
    return ler_config(db, "arquivador_entrada") or ENTRADA_PADRAO


def publicar_ligado(db: Session) -> bool:
    return ler_config(db, "certidoes_publicar") == "1"


def agenda(db: Session) -> tuple[int, str]:
    """(dia da semana 0=segunda … 6=domingo, 'HH:MM'); dia -1 = sem agendamento."""
    v = ler_config(db, "certidoes_agenda") or "4 17:00"
    try:
        d, h = v.split()
        datetime.strptime(h, "%H:%M")
        return int(d), h
    except ValueError:
        return 4, "17:00"


def proxima_execucao(db: Session, agora: datetime | None = None) -> datetime | None:
    d, h = agenda(db)
    if d < 0:
        return None
    agora = agora or datetime.now()
    hh, mm = map(int, h.split(":"))
    alvo = agora.replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(days=(d - agora.weekday()) % 7)
    return alvo if alvo > agora else alvo + timedelta(days=7)


# ---- situação ------------------------------------------------------------------------------

def estado(c: models.Certidao, substituida: bool = False, hoje: date | None = None) -> str:
    hoje = hoje or date.today()
    if substituida:
        return "SUBSTITUIDA"
    if c.vencimento and c.vencimento < hoje:
        return "VENCIDA"
    if not c.disponivel:
        return "INDISPONIVEL"   # "Ainda não baixada", "Não Emitida"… — o Pack não tem o arquivo
    if c.situacao and c.situacao not in T.SITUACOES_OK:
        sit = c.situacao.lower()
        if "não emitida" in sit or "nao emitida" in sit:
            return "NAO_EMITIDA"
        if "impedimento" in sit:
            return "IMPEDIMENTO"
        if any(x in sit for x in ("aguardando", "processamento", "em análise", "em analise", "solicitad", "pendente de emiss")):
            return "AGUARDANDO"   # pedido feito ao órgão e ainda sem resposta: não é positiva
        return "POSITIVA"
    if not c.vencimento:
        return "SEM_VALIDADE"
    if c.vencimento <= hoje + timedelta(days=DIAS_AVISO):
        return "VENCE_LOGO"
    return "VALIDA"


def _ordem(c: models.Certidao):
    return (c.vencimento or date.min, c.emissao or date.min, int(c.pack_id) if c.pack_id.isdigit() else 0)


def atuais(certs: list[models.Certidao]) -> tuple[list[models.Certidao], set[int]]:
    """Separa a certidão mais recente de cada empresa+tipo; devolve (atuais, ids das substituídas)."""
    grupos: dict[tuple, list] = {}
    for c in certs:
        if c.no_pack:
            grupos.setdefault((c.prestador_id, c.tipo_nome), []).append(c)
    atual, subst = [], set()
    for lista in grupos.values():
        lista.sort(key=_ordem, reverse=True)
        atual.append(lista[0])
        subst.update(x.id for x in lista[1:])
    subst.update(c.id for c in certs if not c.no_pack)
    return atual, subst


def marcar_substituidas(db: Session, prestador_id: int | None = None) -> int:
    """Grava no histórico quando e por qual certidão cada uma foi substituída (a data é a emissão da mais nova).
    Idempotente; devolve quantas foram marcadas agora."""
    stmt = select(models.Certidao)
    if prestador_id:
        stmt = stmt.where(models.Certidao.prestador_id == prestador_id)
    certs = db.scalars(stmt).all()
    atual, subst = atuais(certs)
    por_tipo = {(c.prestador_id, c.tipo_nome): c for c in atual}
    n = 0
    for c in certs:
        if c.id in subst and not c.substituida_por:
            nova = por_tipo.get((c.prestador_id, c.tipo_nome))
            if nova is not None and nova.id != c.id:
                c.substituida_por = nova.pack_id
                c.substituida_em = nova.emissao or (nova.alterado_em.date() if nova.alterado_em else None)
            else:   # saiu do Pack sem outra no lugar
                c.substituida_por = "-"
                c.substituida_em = c.alterado_em.date() if c.alterado_em else None
            n += 1
        elif c.id not in subst and c.substituida_por:
            c.substituida_por, c.substituida_em = "", None     # voltou a ser a atual
    db.flush()
    return n


def historico(db: Session, prestador_id: int | None = None) -> list[dict]:
    """Certidões substituídas, agrupáveis por empresa e tipo (mais nova primeiro)."""
    stmt = select(models.Certidao)
    if prestador_id:
        stmt = stmt.where(models.Certidao.prestador_id == prestador_id)
    certs = db.scalars(stmt).all()
    _, subst = atuais(certs)
    out = []
    for c in certs:
        if c.id not in subst:
            continue
        copia = bool(c.arquivo) and (DATA_DIR / c.arquivo).exists()
        out.append(dict(c=c, copia=copia, situacao=c.situacao or "—"))
    out.sort(key=lambda r: (_cod_ord(r["c"].prestador), r["c"].tipo_nome, [-x for x in (_ordem(r["c"])[0].toordinal(),)]))
    return out


def linhas(db: Session, prestador_id: int | None = None) -> list[dict]:
    stmt = select(models.Certidao)
    if prestador_id:
        stmt = stmt.where(models.Certidao.prestador_id == prestador_id)
    certs = db.scalars(stmt).all()
    _, subst = atuais(certs)
    out = []
    for c in certs:
        e = estado(c, c.id in subst)
        out.append(dict(c=c, estado=e, rotulo=ESTADOS[e][0], classe=ESTADOS[e][1], substituida=c.id in subst,
                        acao="" if c.id in subst else o_que_fazer(e, c.tipo or "")))
    out.sort(key=lambda r: (_cod_ord(r["c"].prestador), r["substituida"], r["c"].tipo_nome, -(_ordem(r["c"])[0].toordinal())))
    return out


def _cod_ord(p: models.Prestador):
    return p.ordem


# ---- coleta --------------------------------------------------------------------------------

def empresas_alvo(db: Session, prestador_id: int | None = None) -> list[models.Prestador]:
    stmt = select(models.Prestador).where(models.Prestador.ativo.is_(True), models.Prestador.usa_cnd.is_(True),
                                          models.Prestador.usa_econtador.is_(True))
    if prestador_id:
        stmt = stmt.where(models.Prestador.id == prestador_id)
    return sorted(db.scalars(stmt).all(), key=_cod_ord)


def coletar_empresa(db: Session, cli: ClienteEContador, p: models.Prestador, agora: datetime | None = None) -> dict:
    agora = agora or datetime.now()
    hoje = agora.date()
    vistos, novas, alteradas, baixadas = set(), 0, 0, 0
    for r in cli.certidoes(p.cnpj):
        if r["tipo_nome"] in T.EXCLUIDOS:
            continue
        c = db.scalar(select(models.Certidao).where(models.Certidao.pack_id == r["pack_id"]))
        meta = (r["vencimento"], r["situacao"], r["disponivel"])
        if c is None:
            c = models.Certidao(prestador_id=p.id, pack_id=r["pack_id"], alterado_em=agora)
            db.add(c)
            novas += 1
        elif c.prestador_id != p.id:
            raise ErroAPI(f"Certidão {r['pack_id']} já pertence a outra empresa no sistema; conferir cadastro")
        elif (c.vencimento, c.situacao, c.disponivel) != meta:
            alteradas += 1
            c.alterado_em = agora
            c.arquivo, c.sha256 = "", ""
            c.publicado_em, c.publicado_como = None, ""
        c.tipo_id, c.tipo_nome, c.tipo = r["tipo_id"], r["tipo_nome"][:200], T.tipo_de(r["tipo_nome"])
        c.emissao, c.vencimento, c.situacao, c.disponivel = r["emissao"], r["vencimento"], r["situacao"][:60], r["disponivel"]
        c.avisos = ", ".join(r["avisos"])
        c.no_pack, c.consultado_em = True, agora
        vistos.add(r["pack_id"])
        if c.disponivel and not c.arquivo:   # guarda cópia também das vencidas: vão para o histórico
            try:
                raw, ext = cli.baixar(p.cnpj, c.pack_id, c.vencimento, c.situacao)
                destino = PASTA / p.cnpj / f"{(p.codigo_interno or 'x').zfill(2)}-{c.pack_id}{ext}"
                destino.parent.mkdir(parents=True, exist_ok=True)
                destino.write_bytes(raw)
                c.arquivo = str(destino.relative_to(DATA_DIR))
                c.sha256 = hashlib.sha256(raw).hexdigest()
                c.erro = ""
                baixadas += 1
            except ErroAPI as e:
                c.erro = str(e)[:500]
    for c in db.scalars(select(models.Certidao).where(models.Certidao.prestador_id == p.id)):
        if c.pack_id not in vistos and not c.pack_id.startswith("RFB-"):   # federal emitida pela API não vem do Pack
            c.no_pack = False
    db.flush()
    marcar_substituidas(db, p.id)
    return dict(certidoes=len(vistos), novas=novas, alteradas=alteradas, baixadas=baixadas)


# ---- entrega ao Bot Arquivador -------------------------------------------------------------

def _no_drive(p: models.Prestador, c: models.Certidao, tipos_da_empresa: set[str]) -> str | None:
    """Nome do arquivo da pasta Certidões do Drive igual ou mais novo que esta certidão (None = não achou/sem acesso)."""
    fam = T.familia(c.tipo)
    if not p.pasta_drive or not fam or not c.vencimento:
        return None
    if fam in ("MUNICIPAL", "ESTADUAL", "FALENCIA") and sum(1 for t in tipos_da_empresa if T.familia(t) == fam) > 1:
        return None  # dois municípios/estados: o nome do arquivo não distingue qual é
    pasta = Path(p.pasta_drive) / "04 - Documentos Diversos" / "Certidões"
    try:
        nomes = [f.name for f in pasta.iterdir() if f.is_file()]
    except OSError:
        return None
    for n in nomes:
        info = T.arquivo_local(n)
        if info and info[0] == fam and info[1] >= c.vencimento:
            return n
    return None


def candidatas(db: Session, hoje: date | None = None) -> list[models.Certidao]:
    hoje = hoje or date.today()
    certs = db.scalars(select(models.Certidao)).all()
    atual, _ = atuais(certs)
    publicadas = [c for c in certs if c.publicado_em]
    out = []
    for c in atual:
        if not c.prestador.ativo or not c.prestador.usa_cnd or not c.tipo or c.publicado_em or not c.arquivo:
            continue
        if not c.vencimento or c.vencimento < hoje or c.situacao not in T.SITUACOES_OK:
            continue
        if any(x.prestador_id == c.prestador_id and x.tipo_nome == c.tipo_nome and x.vencimento and x.vencimento >= c.vencimento
               for x in publicadas):
            continue
        out.append(c)
    return out


def publicar(db: Session, c: models.Certidao, pasta_entrada: str, agora: datetime | None = None) -> str:
    """Entrega uma certidão ao Bot Arquivador. Devolve o texto do que foi feito."""
    agora = agora or datetime.now()
    p = c.prestador
    tipos_emp = {x.tipo for x in db.scalars(select(models.Certidao).where(models.Certidao.prestador_id == p.id,
                                                                          models.Certidao.no_pack.is_(True)))}
    ja = _no_drive(p, c, tipos_emp)
    if ja:
        c.publicado_em, c.publicado_como = agora, f"já estava no Drive: {ja}"
        return c.publicado_como
    origem = DATA_DIR / c.arquivo
    if not origem.is_file() or hashlib.sha256(origem.read_bytes()).hexdigest() != c.sha256:
        raise ValueError("Arquivo baixado ausente ou alterado; consultar novamente")
    entrada_dir = Path(pasta_entrada)
    if not entrada_dir.is_dir():
        raise ValueError(f"Pasta de entrada do Bot Arquivador não encontrada: {pasta_entrada}")
    nome = f"CND-{(p.codigo_interno or 'x').zfill(2)}-{c.pack_id}{origem.suffix}"
    alvo = entrada_dir / nome
    temp = entrada_dir / (nome + ".pending")
    if alvo.exists():
        raise ValueError(f"Já existe {nome} na entrada do arquivador (pendente lá?)")
    with origem.open("rb") as src, temp.open("xb") as dst:
        dst.write(src.read())
    if hashlib.sha256(temp.read_bytes()).hexdigest() != c.sha256:
        temp.unlink(missing_ok=True)
        raise ValueError("Cópia para a entrada ficou diferente do original")
    os.replace(temp, alvo)
    c.publicado_em, c.publicado_como, c.erro = agora, str(alvo), ""
    return f"entregue ao arquivador como {nome}"


def publicar_pendentes(db: Session) -> tuple[int, list[str]]:
    feitas, erros = 0, []
    pasta = entrada(db)
    for c in candidatas(db):
        try:
            publicar(db, c, pasta)
            feitas += 1
        except (ValueError, OSError) as e:
            c.erro = str(e)[:500]
            erros.append(f"{c.prestador.razao_social} · {c.tipo_nome}: {e}")
        db.flush()
    return feitas, erros


def marcar_atuais_como_arquivadas(db: Session) -> int:
    """Início de uso: considera já arquivadas as certidões atuais (para não reentregar o que o Monitor já entregou)."""
    n = 0
    agora = datetime.now()
    for c in candidatas(db):
        c.publicado_em, c.publicado_como = agora, "considerada já arquivada (início do uso)"
        n += 1
    return n


# ---- execução (tela, agendamento) -------------------------------------------------------------

def situacao_execucao() -> dict:
    return dict(_execucao)


def executar(prestador_id: int | None = None, usuario: str = "agendador", cli: ClienteEContador | None = None,
             federal_ok: bool = True) -> dict:
    """Roda a coleta (e a entrega, se ligada) com sessão própria. Uma execução por vez."""
    from ..db import SessionLocal
    from ..econtador.servico import cliente as cliente_padrao
    if not _trava.acquire(blocking=False):
        raise ValueError("Já há uma consulta de certidões em andamento.")
    try:
        _execucao.update(rodando=True, inicio=datetime.now(), fim=None, feitas=0, total=0, erros=[], resumo="", por=usuario)
        with SessionLocal() as db:
            from . import federal
            fed_auto = federal_ok and federal.automatico(db)   # federal (Serpro) só agendador/administrador
            try:
                cli = cli or cliente_padrao(db)
                emps = empresas_alvo(db, prestador_id)
            except ErroAPI:
                if not fed_auto:
                    raise
                cli, emps = None, []   # sem e-Contador: faz só a federal
            _execucao["total"] = len(emps)
            soma = dict(certidoes=0, novas=0, alteradas=0, baixadas=0)
            for p in emps:
                try:
                    r = coletar_empresa(db, cli, p)
                    for k in soma:
                        soma[k] += r[k]
                    db.commit()
                except ErroAPI as e:
                    db.rollback()
                    _execucao["erros"].append(f"{p.codigo_interno} {p.razao_social}: {e}")
                    if "401" in str(e) or "conexão" in str(e):
                        break
                _execucao["feitas"] += 1
            fed = dict(consultadas=0, emitidas=0, problemas=[])
            if fed_auto:
                fed = federal.renovar(db, federal.empresas(db, prestador_id), usuario)
                _execucao["erros"].extend(fed["problemas"])
            entregues, erros_pub = 0, []
            if publicar_ligado(db):
                entregues, erros_pub = publicar_pendentes(db)
                _execucao["erros"].extend(erros_pub)
            resumo = (f"{_execucao['feitas']} empresa(s), {soma['certidoes']} certidão(ões), {soma['novas']} nova(s), "
                      f"{soma['alteradas']} alterada(s), {soma['baixadas']} PDF(s) baixado(s), "
                      + (f"{fed['emitidas']} federal(is) emitida(s) na Receita, " if fed_auto else "")
                      + f"{entregues} entregue(s) ao arquivador"
                      + (f", {len(_execucao['erros'])} problema(s)" if _execucao["erros"] else ""))
            _execucao["resumo"] = resumo
            if not prestador_id:
                gravar_config(db, "certidoes_ultima_execucao", datetime.now().isoformat(timespec="seconds"))
            db.add(models.Auditoria(usuario_login=usuario, acao="CERTIDOES_COLETA", entidade="prestador",
                                    entidade_id=prestador_id, detalhes=(resumo + " | " + "; ".join(_execucao["erros"][:20]))[:2000],
                                    data_hora=datetime.now()))
            db.commit()
            return dict(_execucao)
    finally:
        _execucao.update(rodando=False, fim=datetime.now())
        _trava.release()


def executar_em_segundo_plano(prestador_id: int | None, usuario: str, federal_ok: bool = True) -> None:
    if _execucao["rodando"]:
        raise ValueError("Já há uma consulta de certidões em andamento.")

    def alvo():
        try:
            executar(prestador_id, usuario, federal_ok=federal_ok)
        except Exception as e:  # noqa: BLE001
            _execucao["erros"].append(f"Falha: {e}")
            log.exception("coleta de certidões")
    threading.Thread(target=alvo, daemon=True).start()
