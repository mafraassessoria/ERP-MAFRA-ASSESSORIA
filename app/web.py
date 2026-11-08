"""Utilidades comuns das telas: templates, mensagens, usuário logado e auditoria."""
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from . import models
from .db import get_db
from .documentos import formatar_documento

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["doc"] = formatar_documento


def _nome_municipio(codigo):
    from .nfse.tabelas import nome_municipio
    return nome_municipio(codigo) if codigo else ""


templates.env.filters["municipio"] = _nome_municipio
templates.env.filters["data"] = lambda d: d.strftime("%d/%m/%Y") if d else ""
templates.env.filters["datahora"] = lambda d: d.strftime("%d/%m/%Y %H:%M") if d else ""
templates.env.filters["moeda"] = (
    lambda v: f"{Decimal(v or 0):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
)
templates.env.filters["vazio_se_zero"] = (
    lambda v: (f"{Decimal(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") if v else "")
)
templates.env.globals.update(
    REGIMES=models.REGIMES, MODOS=models.MODOS, AMBIENTES=models.AMBIENTES, PERFIS=models.PERFIS,
    SITUACOES_NOTA=models.SITUACOES_NOTA, ORIGENS_NOTA=models.ORIGENS_NOTA,
    TRIBUTACAO_ISSQN=models.TRIBUTACAO_ISSQN, RETENCAO_ISS=models.RETENCAO_ISS,
    REG_AP_TRIB_SN=models.REG_AP_TRIB_SN, REGIMES_CURTO=models.REGIMES_CURTO, PAPEIS_NOTA=models.PAPEIS_NOTA,
)


class PrecisaLogin(Exception):
    pass


class SemPermissao(Exception):
    pass


def flash(request: Request, msg: str, tipo: str = "ok") -> None:
    request.session.setdefault("_flash", []).append([tipo, msg])


def render(request: Request, nome: str, **ctx):
    ctx.setdefault("usuario", getattr(request.state, "usuario", None))
    ctx["mensagens"] = request.session.pop("_flash", []) + ctx.get("mensagens", [])
    # Empresa selecionada: as telas que pertencem a uma empresa mostram a barra dela (com "Sair")
    p = ctx.get("p")
    if isinstance(p, models.Prestador) and p.id:
        request.session["prestador_atual"] = p.id
        ctx["empresa_sel"] = p
    elif request.session.get("prestador_atual") and ctx.get("usuario") is not None:
        # Continua "dentro" da empresa até clicar em Sair da empresa (o menu geral fica escondido)
        from .db import SessionLocal
        with SessionLocal() as db:
            sel = db.get(models.Prestador, request.session["prestador_atual"])
            if sel is None:
                request.session.pop("prestador_atual", None)
            else:
                db.expunge(sel)
                ctx["empresa_sel"] = sel
    return templates.TemplateResponse(request, nome, ctx)


def usuario_atual(request: Request, db: Session = Depends(get_db)) -> models.Usuario:
    uid = request.session.get("uid")
    u = db.get(models.Usuario, uid) if uid else None
    if not u or not u.ativo:
        request.session.clear()
        raise PrecisaLogin()
    request.state.usuario = u
    return u


def admin(u: models.Usuario = Depends(usuario_atual)) -> models.Usuario:
    if u.perfil != "ADMIN":
        raise SemPermissao()
    return u


def ip_de(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")


def auditar(db: Session, request: Request, usuario: models.Usuario | None, acao: str,
            entidade: str = "", entidade_id: int | None = None, detalhes: str = "") -> None:
    db.add(models.Auditoria(
        usuario_id=usuario.id if usuario else None,
        usuario_login=usuario.login if usuario else "",
        acao=acao, entidade=entidade, entidade_id=entidade_id,
        detalhes=detalhes[:2000], ip=ip_de(request), data_hora=datetime.now(),
    ))


def texto(form, campo: str, maximo: int | None = None) -> str:
    v = (form.get(campo) or "").strip()
    return v[:maximo] if maximo else v


def decimal_br(valor: str) -> Decimal:
    """Aceita '1.234,56' ou '1234.56'."""
    v = (valor or "").strip()
    if not v:
        return Decimal("0")
    if "," in v:
        v = v.replace(".", "").replace(",", ".")
    try:
        return Decimal(v)
    except InvalidOperation:
        raise ValueError(f"Valor inválido: {valor}")
