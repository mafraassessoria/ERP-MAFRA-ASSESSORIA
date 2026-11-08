"""Chave da API (criptografada) e sincronização do cadastro de empresas com o e-Contador."""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..configuracoes import gravar_config, ler_config
from ..seguranca import criptografar, descriptografar
from .cliente import ClienteEContador, ErroAPI

CHAVE_TOKEN = "econtador_token_cripto"


def gravar_token(db: Session, token: str) -> None:
    token = (token or "").strip()
    if token:
        gravar_config(db, CHAVE_TOKEN, criptografar(token.encode()).decode())


def tem_token(db: Session) -> bool:
    return bool(ler_config(db, CHAVE_TOKEN))


def cliente(db: Session) -> ClienteEContador:
    v = ler_config(db, CHAVE_TOKEN)
    if not v:
        raise ErroAPI("Chave da API do e-Contador não configurada (Configurações → e-Contador).")
    return ClienteEContador(descriptografar(v.encode()).decode())


@dataclass
class ResultadoSinc:
    vinculadas: list[str] = field(default_factory=list)
    ja_vinculadas: int = 0
    divergentes: list[str] = field(default_factory=list)
    so_no_econtador: list[str] = field(default_factory=list)
    sem_econtador: list[str] = field(default_factory=list)

    @property
    def resumo(self) -> str:
        return (f"{len(self.vinculadas)} vinculada(s) agora, {self.ja_vinculadas} já vinculada(s), "
                f"{len(self.divergentes)} divergência(s), {len(self.so_no_econtador)} só no e-Contador, "
                f"{len(self.sem_econtador)} marcada(s) com e-Contador mas não encontrada(s) lá")


def _cod(v) -> str:
    s = str(v or "").strip()
    return str(int(s)) if s.isdigit() else ""


def sincronizar_empresas(db: Session, cli: ClienteEContador | None = None) -> ResultadoSinc:
    """Liga cada empresa do sistema à do e-Contador pelo CNPJ e confere o código interno (externoid).

    Não cria nem apaga empresas: só grava o ID do e-Contador e relata divergências para conferência."""
    cli = cli or cliente(db)
    remotas = cli.empresas()
    res = ResultadoSinc()
    locais = db.scalars(select(models.Prestador)).all()
    por_cnpj = {}
    for p in locais:
        por_cnpj[p.cnpj] = p
        for c in p.lista_cnpjs_adicionais:
            por_cnpj.setdefault(c, p)
    usados = set()
    for r in remotas:
        rot = f"{r['codigo'] or '?'} — {r['nome']}"
        p = por_cnpj.get(r["cnpj"] or "")
        if p is None:
            res.so_no_econtador.append(f"{rot} ({r['cnpj'] or 'sem CNPJ'})")
            continue
        usados.add(p.id)
        if r["codigo"] and _cod(p.codigo_interno) and _cod(p.codigo_interno) != r["codigo"]:
            res.divergentes.append(f"{rot}: no sistema o código é {p.codigo_interno} ({p.razao_social})")
        elif r["codigo"] and not p.codigo_interno:
            p.codigo_interno = r["codigo"]
        if p.id_econtador == r["id"]:
            res.ja_vinculadas += 1
        else:
            p.id_econtador = r["id"]
            res.vinculadas.append(rot)
    for p in locais:
        if p.ativo and p.usa_econtador and p.id not in usados:
            res.sem_econtador.append(f"{p.codigo_interno or '?'} — {p.razao_social} ({p.cnpj})")
    return res
