"""Execução das rotinas de acompanhamento (manual ou agendada), estimativa de custo e dados do painel."""
from __future__ import annotations

import logging
import threading
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..configuracoes import gravar_config, ler_config
from ..serpro import servico as sv
from .rotinas import POR_ROTINA, ROTINAS

log = logging.getLogger(__name__)
DIAS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
_exec = {"rodando": False, "rotina": "", "feitas": 0, "total": 0, "erros": [], "resumo": "", "inicio": None, "fim": None,
         "por": ""}
_trava = threading.Lock()


# ---- agenda ------------------------------------------------------------------------------------

def agenda(db: Session, cod: str) -> tuple[bool, str]:
    """(ligada, regra) — regra: 'S<0-6> HH:MM' semanal, 'M<1-28> HH:MM' mensal, 'A<MM-DD> HH:MM' anual."""
    r = POR_ROTINA[cod]
    return ler_config(db, f"monitor_ativo_{cod}") == "1", ler_config(db, f"monitor_agenda_{cod}") or r.agenda_padrao


def validar_regra(regra: str) -> str:
    regra = " ".join((regra or "").split())
    try:
        quando, hora = regra.split(" ")
        datetime.strptime(hora, "%H:%M")
        tipo, v = quando[0].upper(), quando[1:]
        if tipo == "S":
            assert 0 <= int(v) <= 6
        elif tipo == "M":
            assert 1 <= int(v) <= 28
        elif tipo == "A":
            datetime.strptime("2001-" + v, "%Y-%m-%d")
        else:
            raise ValueError
    except (ValueError, AssertionError, IndexError):
        raise ValueError("Agenda inválida") from None
    return f"{tipo}{v} {hora}"


def descrever(regra: str) -> str:
    quando, hora = regra.split(" ")
    t, v = quando[0], quando[1:]
    if t == "S":
        return f"toda {DIAS[int(v)]}{'-feira' if int(v) < 5 else ''} às {hora}"
    if t == "M":
        return f"todo dia {int(v)} às {hora}"
    return f"todo ano em {v[3:]}/{v[:2]} às {hora}"


def momento_anterior(regra: str, agora: datetime) -> datetime:
    quando, hora = regra.split(" ")
    hh, mm = map(int, hora.split(":"))
    t, v = quando[0], quando[1:]
    base = agora.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if t == "S":
        m = base - timedelta(days=(agora.weekday() - int(v)) % 7)
        return m if m <= agora else m - timedelta(days=7)
    if t == "M":
        m = base.replace(day=int(v))
        if m > agora:
            m = (m.replace(day=1) - timedelta(days=1)).replace(day=int(v))
        return m
    mes, dia = map(int, v.split("-"))
    m = base.replace(month=mes, day=dia)
    return m if m <= agora else m.replace(year=agora.year - 1)


def proxima(regra: str, agora: datetime) -> datetime:
    ant = momento_anterior(regra, agora)
    t = regra[0]
    if t == "S":
        return ant + timedelta(days=7)
    if t == "M":
        return (ant.replace(day=1) + timedelta(days=32)).replace(day=ant.day)
    return ant.replace(year=ant.year + 1)


def vencidas(db: Session, agora: datetime | None = None) -> list[str]:
    """Rotinas ligadas cujo horário passou hoje e que ainda não rodaram desde então."""
    agora = agora or datetime.now()
    out = []
    for r in ROTINAS:
        ligada, regra = agenda(db, r.codigo)
        if not ligada:
            continue
        alvo = momento_anterior(regra, agora)
        ult = ler_config(db, f"monitor_ultima_{r.codigo}")
        try:
            ult_dt = datetime.fromisoformat(ult) if ult else None
        except ValueError:
            ult_dt = None
        if alvo.date() == agora.date() and (ult_dt is None or ult_dt < alvo):
            out.append(r.codigo)
    return out


# ---- empresas e custo ---------------------------------------------------------------------------

def empresas(db: Session, cod: str, ids: list[int] | None = None) -> list[models.Prestador]:
    r = POR_ROTINA[cod]
    stmt = select(models.Prestador).where(models.Prestador.ativo.is_(True), models.Prestador.usa_receita.is_(True))
    if ids:
        stmt = stmt.where(models.Prestador.id.in_(ids))
    return [p for p in db.scalars(stmt.order_by(*models.ordem_prestador_sql())) if r.aplica(p)]


def custo_estimado(db: Session, cod: str, n_empresas: int) -> Decimal:
    from ..serpro.catalogo import POR_CODIGO
    if sv.modo(db) != "producao":
        return Decimal("0")
    r = POR_ROTINA[cod]
    unit = sum((sv.custo_previsto(db, POR_CODIGO[s]) for s in r.servicos), Decimal("0"))
    if cod == "DCTFWEB":
        unit = unit * Decimal("1.2")  # meses em aberto reconsultados
    return (unit * n_empresas).quantize(Decimal("0.01"))


# ---- execução -------------------------------------------------------------------------------------

def situacao() -> dict:
    return dict(_exec)


def executar(cod: str, usuario: str, ids: list[int] | None = None, cli=None, hoje: date | None = None) -> dict:
    from ..db import SessionLocal
    if not _trava.acquire(blocking=False):
        raise ValueError("Já há uma rotina de acompanhamento em andamento.")
    r = POR_ROTINA[cod]
    try:
        _exec.update(rodando=True, rotina=r.nome, feitas=0, total=0, erros=[], resumo="", inicio=datetime.now(), fim=None, por=usuario)
        with SessionLocal() as db:
            cli = cli or sv.cliente(db)
            emps = empresas(db, cod, ids)
            _exec["total"] = len(emps)
            lote = f"MON-{cod}-{datetime.now():%Y%m%d-%H%M}"
            cont = Counter()
            for p in emps:
                try:
                    m = r.executar(db, p, usuario, lote, cli, hoje or date.today())
                    cont[m.status if m else "?"] += 1
                    db.commit()
                except sv.ErroConsulta as e:
                    db.rollback()
                    _exec["erros"].append(f"{p.razao_social}: {e}")
                    if "Limite de gasto" in str(e):
                        break
                except Exception as e:  # noqa: BLE001
                    db.rollback()
                    log.exception("monitor %s %s", cod, p.cnpj)
                    _exec["erros"].append(f"{p.razao_social}: falha inesperada ({type(e).__name__})")
                _exec["feitas"] += 1
            resumo = f"{r.nome}: {_exec['feitas']} empresa(s) — " + ", ".join(f"{k} {v}" for k, v in cont.items())
            if cod == "PAGAMENTOS":  # baixa automática das guias geradas pelo sistema
                from ..guias.servico import conciliar
                b = conciliar(db)
                resumo += f" · {b['pagas']} guia(s) baixada(s)" + (f", {len(b['conferir'])} para conferir" if b["conferir"] else "")
                from ..econtador import gravacoes as ecg
                if ecg.automatico(db, "econtador_baixa_guias_auto"):
                    eb = ecg.baixar_guias_pagas(db)
                    resumo += f" · {eb['baixadas']} baixada(s) no e-Contador"
                    _exec["erros"].extend(f"e-Contador: {x}" for x in eb["problemas"][:10])
            _exec["resumo"] = resumo
            if not ids:
                gravar_config(db, f"monitor_ultima_{cod}", datetime.now().isoformat(timespec="seconds"))
            db.add(models.Auditoria(usuario_login=usuario, acao="MONITOR_" + cod, entidade="prestador",
                                    detalhes=(resumo + " | " + "; ".join(_exec["erros"][:20]))[:2000], data_hora=datetime.now()))
            db.commit()
            return dict(_exec)
    finally:
        _exec.update(rodando=False, fim=datetime.now())
        _trava.release()


def executar_em_segundo_plano(cod: str, usuario: str, ids: list[int] | None = None) -> None:
    if _exec["rodando"]:
        raise ValueError("Já há uma rotina de acompanhamento em andamento.")

    def alvo():
        try:
            executar(cod, usuario, ids)
        except Exception as e:  # noqa: BLE001
            _exec["erros"].append(f"Falha: {e}")
            log.exception("monitor")
    threading.Thread(target=alvo, daemon=True).start()


def executar_agendadas(agora: datetime | None = None) -> list[str]:
    """Chamado pelo agendador: roda, uma de cada vez, as rotinas cujo horário chegou."""
    from ..db import SessionLocal
    with SessionLocal() as db:
        if sv.modo(db) == "desligado":
            return []
        pend = vencidas(db, agora)
    if not pend or _exec["rodando"]:
        return []

    def alvo():
        for cod in pend:
            try:
                executar(cod, "agendador")
            except Exception:  # noqa: BLE001
                log.exception("monitor agendado %s", cod)
    threading.Thread(target=alvo, daemon=True).start()
    return pend


# ---- painel ---------------------------------------------------------------------------------------

STATUS = {
    "REGULAR": ("Regular", "ok"), "PENDENCIA": ("Com pendência", "erro"), "ENTREGUE": ("Entregue", "ok"),
    "NAO_ENTREGUE": ("Não entregue", "erro"), "COM": ("Com", "aviso"), "SEM": ("Sem", "ok"), "NOVAS": ("Não lidas", "aviso"),
    "ERRO": ("Erro na consulta", "erro"), "INDEFINIDO": ("Conferir", "aviso"),
}
# Situações que cada acompanhamento pode ter (o filtro mostra só estas; Erro/Conferir só quando existirem)
STATUS_POR_ITEM = {
    "SITFIS": ["PENDENCIA", "INDEFINIDO", "REGULAR", "ERRO"],
    "PARCELAMENTO": ["COM", "SEM", "ERRO"],
    "CAIXA": ["NOVAS", "SEM", "ERRO"],
    "PAGAMENTOS": ["COM", "SEM", "ERRO"],
    "PGDASD": ["ENTREGUE", "NAO_ENTREGUE", "ERRO"], "DCTFWEB": ["ENTREGUE", "NAO_ENTREGUE", "ERRO"],
    "MIT": ["ENTREGUE", "NAO_ENTREGUE", "ERRO"], "DEFIS": ["ENTREGUE", "NAO_ENTREGUE", "ERRO"],
}
# Rótulos próprios de um acompanhamento (Parcelamentos: tem parcelamento ativo? Sim / Não)
ROTULO_STATUS_ITEM = {"PARCELAMENTO": {"COM": ("Sim", "aviso"), "SEM": ("Não", "ok")}}


def status_de(item: str, status: str) -> tuple[str, str]:
    return ROTULO_STATUS_ITEM.get(item, {}).get(status) or STATUS.get(status, (status, ""))


def opcoes_status(item: str, presentes) -> list[tuple[str, str]]:
    """(código, rótulo) para o filtro: as situações aplicáveis ao item; Erro e Conferir só se houver."""
    presentes = set(presentes)
    cods = [c for c in STATUS_POR_ITEM.get(item, list(STATUS)) if c not in ("ERRO", "INDEFINIDO") or c in presentes]
    cods += sorted(c for c in presentes if c not in cods)
    return [(c, status_de(item, c)[0]) for c in cods]


ROTULO_ITEM = {"SITFIS": "Situação fiscal", "PARCELAMENTO": "Parcelamentos", "CAIXA": "Caixa postal", "PAGAMENTOS": "Pagamentos",
               "PGDASD": "PGDAS-D", "DCTFWEB": "DCTFWeb", "MIT": "MIT", "DEFIS": "DEFIS"}


def referencia_padrao(item: str, hoje: date | None = None) -> str:
    from .rotinas import mes_anterior
    hoje = hoje or date.today()
    if item in ("PGDASD", "DCTFWEB", "MIT"):
        return mes_anterior(hoje)
    if item == "DEFIS":
        return str(hoje.year - 1)
    return ""


def itens(db: Session, item: str, referencia: str | None = None) -> list[models.MonitorItem]:
    ref = referencia_padrao(item) if referencia is None else referencia
    stmt = select(models.MonitorItem).where(models.MonitorItem.item == item)
    if item != "PAGAMENTOS":
        stmt = stmt.where(models.MonitorItem.referencia == ref)
    lista = [m for m in db.scalars(stmt) if m.prestador.ativo and m.prestador.usa_receita]
    if item == "PAGAMENTOS":  # o mais recente por empresa
        ult = {}
        for m in lista:
            if m.prestador_id not in ult or m.atualizado_em > ult[m.prestador_id].atualizado_em:
                ult[m.prestador_id] = m
        lista = list(ult.values())
    return sorted(lista, key=lambda m: m.prestador.ordem)


def painel(db: Session) -> dict:
    out = {}
    for cod in ROTULO_ITEM:
        lista = itens(db, cod)
        out[cod] = dict(ref=referencia_padrao(cod), total=len(lista), cont=Counter(m.status for m in lista))
    hoje = date.today()
    inicio = (hoje.replace(day=1) - timedelta(days=1)).replace(day=1)
    pgs = db.scalars(select(models.PagamentoRF).where(models.PagamentoRF.data_arrecadacao >= inicio)).all()
    out["pagamentos_mes"] = dict(total=len(pgs), atraso=sum(1 for p in pgs if p.em_atraso),
                                 valor=sum((p.valor for p in pgs), Decimal("0")))
    return out



def atrasos_sitfis(db: Session, prestador_id: int | None = None) -> dict[int, dict]:
    """Parcelas em atraso informadas na última situação fiscal (sem custo extra): {prestador_id: {itens, quando}}.
    itens = [(parcelamento, número de parcelas em atraso)]."""
    import json as _json
    import re as _re
    stmt = select(models.MonitorItem).where(models.MonitorItem.item == "SITFIS")
    if prestador_id:
        stmt = stmt.where(models.MonitorItem.prestador_id == prestador_id)
    out = {}
    for m in db.scalars(stmt):
        try:
            d = _json.loads(m.detalhe) if m.detalhe else {}
        except ValueError:
            continue
        itens = []
        for x in d.get("detalhes") or []:
            tit = str(x.get("titulo", ""))
            if not tit.lower().startswith("parcelamento"):
                continue
            n = _re.search(r"Parcelas em atraso\s+(\d+)", " ".join(str(l) for l in x.get("linhas") or []), _re.I)
            itens.append((_re.sub(r"^Parcelamento\s*", "", tit).strip("() ") or tit, int(n.group(1)) if n else None))
        if itens:
            out[m.prestador_id] = {"itens": itens, "quando": m.atualizado_em}
    return out
