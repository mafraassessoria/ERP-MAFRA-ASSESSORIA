"""Painel inicial: faixa "Para hoje" (o que pede ação, de todos os módulos) e um cartão por módulo.

Cada número é um link para a tela já filtrada. Módulos sem nenhuma empresa ligada não aparecem;
o cartão Sistema (gasto Serpro, backup, segurança, versão) só para administrador.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models
from .config import BACKUP_DIR, DIAS_AVISO_CERTIFICADO

DIAS_SEMANA = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]


def _brl(v) -> str:
    v = Decimal(v or 0)
    if v >= 100_000:
        return f"R$ {v / 1000:,.0f} mil".replace(",", ".")
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _quando(dt) -> str:
    if not dt:
        return "nunca"
    hoje = date.today()
    d = dt.date() if isinstance(dt, datetime) else dt
    hora = f" {dt:%H:%M}" if isinstance(dt, datetime) else ""
    if d == hoje:
        return "hoje" + hora
    if d == hoje - timedelta(days=1):
        return "ontem" + hora
    return f"{d:%d/%m}" + hora


def _linha(rotulo, valor, link="", nivel=""):
    return dict(rotulo=rotulo, valor=valor, link=link, nivel=nivel)


def montar(db: Session, u: models.Usuario) -> dict:
    hoje = date.today()
    ini_mes = hoje.replace(day=1)
    acoes: list[dict] = []          # nivel: erro | aviso | info
    cartoes: list[dict] = []
    detalhes_nfse: list[tuple] = []  # pendências por empresa (certificado, município…)

    def acao(nivel, n, texto, link):
        if n:
            acoes.append(dict(nivel=nivel, n=n, texto=texto, link=link))

    prest = db.scalars(select(models.Prestador).where(models.Prestador.ativo.is_(True))).all()

    # ---- NFS-e ------------------------------------------------------------------------------
    if any(p.usa_nfse for p in prest):
        from .nfse.emissao import descricao_certificado
        limite = datetime.now() + timedelta(days=DIAS_AVISO_CERTIFICADO)
        cert_venc = cert_logo = 0
        for p in prest:
            if not p.usa_nfse:
                continue
            if p.importar_por_procuracao:
                _dono, problema = descricao_certificado(db, p)
                if problema:
                    detalhes_nfse.append((p, problema, None))
                c = None
            else:
                c = p.certificado_ativo
            if c is None:
                if not p.importar_por_procuracao:
                    detalhes_nfse.append((p, "sem certificado A1 cadastrado", None))
            elif c.validade_fim < datetime.now():
                cert_venc += 1
                detalhes_nfse.append((p, f"certificado VENCIDO em {c.validade_fim:%d/%m/%Y}", None))
            elif c.validade_fim < limite:
                cert_logo += 1
                detalhes_nfse.append((p, f"certificado vence em {c.dias_para_vencer} dias ({c.validade_fim:%d/%m/%Y})", None))
            if p.modo == "EMISSAO":
                if not p.municipio_aderente:
                    detalhes_nfse.append((p, "município não confirmado como aderente ao Emissor Nacional", None))
                if not p.autorizacao_uso_certificado and not p.importar_por_procuracao:
                    detalhes_nfse.append((p, "sem autorização registrada para uso do certificado", None))
        problemas = db.scalars(select(models.NotaFiscal).where(
            models.NotaFiscal.situacao.in_(("PROCESSANDO", "REJEITADA")), models.NotaFiscal.ambiente == "PRODUCAO")).all()
        for n in problemas:
            detalhes_nfse.append((n.prestador, f"nota de R$ {n.valor_servicos:.2f} para {n.tomador_nome} está "
                                  f"{models.SITUACOES_NOTA[n.situacao].lower()} (rascunho #{n.id})", n.id))
        acao("erro", cert_venc, "certificado(s) A1 vencido(s)", "#pendencias-nfse")
        acao("aviso", cert_logo, f"certificado(s) A1 vencem em até {DIAS_AVISO_CERTIFICADO} dias", "#pendencias-nfse")
        acao("erro", len(problemas), "nota(s) rejeitada(s) ou sem resposta da Sefin", "/notas?situacao=REJEITADA")
        aut = select(models.NotaFiscal).where(models.NotaFiscal.situacao == "AUTORIZADA", models.NotaFiscal.ambiente == "PRODUCAO",
                                              models.NotaFiscal.papel == "PRESTADO", models.NotaFiscal.competencia >= ini_mes)
        notas_mes = db.scalars(aut).all()
        total_mes = sum((n.valor_servicos or 0 for n in notas_mes), Decimal("0"))
        parados = db.scalar(select(func.count(models.NotaFiscal.id)).where(
            models.NotaFiscal.situacao == "RASCUNHO", models.NotaFiscal.criado_em < datetime.now() - timedelta(days=2))) or 0
        ult_adn = db.scalar(select(func.max(models.ControleNSU.ultima_consulta)))
        cartoes.append(dict(titulo="NFS-e", icone="📄", link="/notas", linhas=[
            _linha("Prestadas e autorizadas no mês", f"{len(notas_mes)} · {_brl(total_mes)}",
                   f"/notas?situacao=AUTORIZADA&papel=PRESTADO&competencia={hoje:%Y-%m}"),
            _linha("Rascunhos parados há mais de 2 dias", parados, "/notas?situacao=RASCUNHO", "aviso" if parados else ""),
            _linha("Rejeitadas ou em processamento", len(problemas), "/notas?situacao=REJEITADA", "erro" if problemas else "ok"),
            _linha("Pendências de cadastro (certificado…)", len([d for d in detalhes_nfse if not d[2]]), "#pendencias-nfse",
                   "aviso" if any(not d[2] for d in detalhes_nfse) else "ok"),
            _linha("Última importação ADN", _quando(ult_adn), "/relatorios"),
        ]))

    # ---- Monitor e-CAC ------------------------------------------------------------------------
    if any(p.usa_receita for p in prest):
        from .monitor import servico as ms
        pn = ms.painel(db)
        cont = {k: v["cont"] for k, v in pn.items() if isinstance(v, dict) and "cont" in v}
        sit = cont.get("SITFIS", Counter())["PENDENCIA"]
        caixa = cont.get("CAIXA", Counter())["NOVAS"]
        pgdas = cont.get("PGDASD", Counter())["NAO_ENTREGUE"]
        dctf = cont.get("DCTFWEB", Counter())["NAO_ENTREGUE"]
        atraso = pn["pagamentos_mes"]["atraso"]
        ult = db.scalar(select(func.max(models.MonitorItem.atualizado_em)))
        acao("erro", sit, "empresa(s) com pendência na situação fiscal", "/monitor/item/SITFIS?status=PENDENCIA")
        acao("aviso", pgdas, f"PGDAS-D não entregue(s) ({pn['PGDASD']['ref']})", "/monitor/item/PGDASD?status=NAO_ENTREGUE")
        acao("aviso", dctf, f"DCTFWeb não entregue(s) ({pn['DCTFWEB']['ref']})", "/monitor/item/DCTFWEB?status=NAO_ENTREGUE")
        acao("info", caixa, "empresa(s) com mensagem não lida na caixa postal", "/monitor/item/CAIXA?status=NOVAS")
        cartoes.append(dict(titulo="Monitor e-CAC", icone="🏛️", link="/monitor", linhas=[
            _linha("Situação fiscal com pendência", sit, "/monitor/item/SITFIS?status=PENDENCIA", "erro" if sit else "ok"),
            _linha(f"PGDAS-D não entregue ({pn['PGDASD']['ref']})", pgdas, "/monitor/item/PGDASD?status=NAO_ENTREGUE", "aviso" if pgdas else "ok"),
            _linha(f"DCTFWeb não entregue ({pn['DCTFWEB']['ref']})", dctf, "/monitor/item/DCTFWEB?status=NAO_ENTREGUE", "aviso" if dctf else "ok"),
            _linha("Caixa postal com não lidas", caixa, "/monitor/item/CAIXA?status=NOVAS", "aviso" if caixa else ""),
            _linha("Pagamentos em atraso (2 meses)", atraso, "/monitor/pagamentos", "erro" if atraso else ""),
            _linha("Última coleta", _quando(ult), "/monitor"),
        ]))

        # ---- Guias ----------------------------------------------------------------------------
        abertas = db.scalars(select(models.GuiaRF).where(models.GuiaRF.situacao == "ABERTA")).all()
        vencidas = [g for g in abertas if g.vencimento and g.vencimento < hoje]
        a_vencer = [g for g in abertas if g.vencimento and hoje <= g.vencimento <= hoje + timedelta(days=5)]
        pagas_mes = db.scalars(select(models.GuiaRF).where(models.GuiaRF.situacao == "PAGA", models.GuiaRF.pago_em >= ini_mes)).all()
        auto = sum(1 for g in pagas_mes if g.pagamento_id)
        ec_pend = db.scalar(select(func.count(models.GuiaRF.id)).where(
            models.GuiaRF.situacao == "PAGA", models.GuiaRF.ec_doc_id != "", models.GuiaRF.ec_baixada_em.is_(None),
            models.GuiaRF.ec_excluida_em.is_(None))) or 0
        acao("aviso", ec_pend, "guia(s) paga(s) ainda em aberto no e-Contador", "/guias?situacao=PAGA#econtador")
        acao("erro", len(vencidas), "guia(s) com o prazo de pagamento passado e ainda em aberto", "/guias?situacao=ABERTA")
        acao("aviso", len(a_vencer), "guia(s) vencem nos próximos 5 dias", "/guias?situacao=ABERTA")
        cartoes.append(dict(titulo="Guias", icone="🧾", link="/guias", linhas=[
            _linha("Em aberto com prazo passado", f"{len(vencidas)} · {_brl(sum((g.valor for g in vencidas), Decimal(0)))}" if vencidas else 0,
                   "/guias?situacao=ABERTA", "erro" if vencidas else "ok"),
            _linha("Em aberto (todas)", len(abertas), "/guias?situacao=ABERTA"),
            _linha("Pagas no mês", f"{len(pagas_mes)} ({auto} pela conciliação)", "/guias?situacao=PAGA", "ok" if pagas_mes else ""),
            _linha("Pagas sem baixa no e-Contador", ec_pend, "/guias?situacao=PAGA#econtador", "aviso" if ec_pend else ""),
        ]))

    # ---- Certidões ---------------------------------------------------------------------------
    if any(p.usa_cnd for p in prest):
        from .certidoes import servico as cs
        linhas_c = [r for r in cs.linhas(db) if not r["substituida"] and r["c"].prestador.ativo]
        ce = Counter(r["estado"] for r in linhas_c)
        fed_mes = db.scalar(select(func.count(models.ConsultaAPI.id)).where(
            models.ConsultaAPI.produto == "CND", models.ConsultaAPI.ok.is_(True),
            models.ConsultaAPI.data_hora >= datetime.combine(ini_mes, datetime.min.time()))) or 0
        ruins = ce["VENCIDA"] + ce["POSITIVA"]
        acao("erro", ruins, "certidão(ões) vencida(s) ou positiva(s)", "/certidoes?estado=VENCIDA")
        acao("aviso", ce["VENCE_LOGO"], "certidão(ões) vencem em até 15 dias", "/certidoes?estado=VENCE_LOGO")
        cartoes.append(dict(titulo="Certidões", icone="📜", link="/certidoes", linhas=[
            _linha("Válidas", ce["VALIDA"], "/certidoes?estado=VALIDA", "ok"),
            _linha("Vencem em até 15 dias", ce["VENCE_LOGO"], "/certidoes?estado=VENCE_LOGO", "aviso" if ce["VENCE_LOGO"] else ""),
            _linha("Vencidas", ce["VENCIDA"], "/certidoes?estado=VENCIDA", "erro" if ce["VENCIDA"] else ""),
            _linha("Positivas — conferir", ce["POSITIVA"], "/certidoes?estado=POSITIVA", "erro" if ce["POSITIVA"] else ""),
            _linha("Federais emitidas na Receita no mês", fed_mes, "/certidoes"),
            _linha("Última consulta", _quando(cs._execucao.get("fim")) if cs._execucao.get("fim") else "—", "/certidoes"),
        ]))

    # ---- Honorários ----------------------------------------------------------------------------
    ult_rec = db.scalar(select(func.max(models.Recebimento.pagamento)))
    if ult_rec:
        ini = ult_rec.replace(day=1)
        fim = (ini.replace(day=28) + timedelta(days=4)).replace(day=1)
        recs = db.scalars(select(models.Recebimento).where(models.Recebimento.pagamento >= ini,
                                                           models.Recebimento.pagamento < fim)).all()
        sem = [r for r in recs if not r.nota_id]
        ult_imp = db.scalar(select(func.max(models.Recebimento.importado_em)))
        mes = f"{ini:%m/%Y}"
        acao("aviso", len(sem), f"recebimento(s) de honorários de {mes} sem nota", f"/honorarios?mes={ini:%Y-%m}&situacao=sem_nota")
        cartoes.append(dict(titulo="Honorários", icone="💰", link="/honorarios", linhas=[
            _linha(f"Recebidos em {mes}", f"{len(recs)} · {_brl(sum((r.recebido or r.valor for r in recs), Decimal(0)))}",
                   f"/honorarios?mes={ini:%Y-%m}"),
            _linha("Sem nota fiscal", len(sem), f"/honorarios?mes={ini:%Y-%m}&situacao=sem_nota", "aviso" if sem else "ok"),
            _linha("Com nota gerada", len(recs) - len(sem), f"/honorarios?mes={ini:%Y-%m}&situacao=com_nota"),
            _linha("Sem baixa no e-Contador", sum(1 for r in recs if not r.ec_baixado_em), f"/honorarios?mes={ini:%Y-%m}",
                   "aviso" if any(not r.ec_baixado_em for r in recs) else "ok"),
            _linha("Último arquivo importado", _quando(ult_imp), "/honorarios"),
        ]))

    # ---- Sistema (administrador) --------------------------------------------------------------
    zips = sorted(BACKUP_DIR.glob("emissor_nfse_*.zip"), key=lambda f: f.stat().st_mtime)
    ultimo_backup = datetime.fromtimestamp(zips[-1].stat().st_mtime) if zips else None
    backup_atrasado = ultimo_backup is None or ultimo_backup < datetime.now() - timedelta(days=2)
    acao("erro", 1 if backup_atrasado else 0, "backup atrasado: " + (f"o último é de {ultimo_backup:%d/%m %H:%M}" if ultimo_backup else "nenhum backup feito"), "/configuracoes")
    seguranca = None
    if u.perfil == "ADMIN":
        from .seguranca import login_bloqueado
        from .serpro import servico as sv
        from .web import versao_sistema
        falhas = db.scalars(select(models.Auditoria).where(models.Auditoria.acao == "LOGIN_FALHOU",
                                                           models.Auditoria.data_hora >= datetime.now() - timedelta(days=1))).all()
        logins = sorted({f.detalhes.split()[0].removeprefix("login=") for f in falhas if f.detalhes})
        bloqueados = [x for x in logins if login_bloqueado(db, x)]
        seguranca = dict(falhas=len(falhas), logins=logins, bloqueados=bloqueados, sem_2fa=not u.tem_2fa)
        acao("erro", len(bloqueados), "usuário(s) bloqueado(s) agora por senha errada: " + ", ".join(bloqueados), "/auditoria?acao=LOGIN_FALHOU")
        gasto, limite = sv.gasto_mes(db), sv.limite_mensal(db)
        perto = limite and gasto >= limite * Decimal("0.8")
        acao("aviso", 1 if perto else 0, f"gasto com o Serpro em {gasto / limite * 100:.0f}% do limite do mês" if limite else "", "/configuracoes#receita")
        cartoes.append(dict(titulo="Sistema", icone="⚙️", link="/configuracoes", linhas=[
            _linha("Gasto Serpro no mês", f"{_brl(gasto)} de {_brl(limite)}", "/configuracoes#apis-serpro", "aviso" if perto else ""),
            _linha("Último backup", _quando(ultimo_backup), "/configuracoes", "erro" if backup_atrasado else "ok"),
            _linha("Logins com erro em 24 h", len(falhas), "/auditoria?acao=LOGIN_FALHOU", "erro" if bloqueados else ("aviso" if falhas else "ok")),
            _linha("Verificação em duas etapas", "ativada" if u.tem_2fa else "desligada", "/minha-conta/2fa", "ok" if u.tem_2fa else "aviso"),
            _linha("Versão", versao_sistema() or "—", ""),
        ]))

    ordem = {"erro": 0, "aviso": 1, "info": 2}
    acoes.sort(key=lambda a: ordem[a["nivel"]])
    return dict(acoes=acoes, cartoes=cartoes, detalhes_nfse=detalhes_nfse, seguranca=seguranca,
                hoje_txt=f"{DIAS_SEMANA[hoje.weekday()]}, {hoje:%d/%m/%Y}",
                empresas=dict(ativas=len(prest), nfse=sum(1 for p in prest if p.usa_nfse),
                              receita=sum(1 for p in prest if p.usa_receita), cnd=sum(1 for p in prest if p.usa_cnd)))
