"""Importação do cadastro de empresas do Bot Arquivador (empresas.json) para o cadastro geral.

Regras (não destrutivas):
- localiza a empresa pelo CNPJ (principal ou adicional); se não achar, pelo código interno;
- código igual com CNPJ diferente = conflito: não altera nada, só relata;
- atualiza a pasta do Drive, acrescenta CNPJs e apelidos, preenche o código interno se estiver vazio;
- nunca altera razão social, CNPJ principal, regime nem configurações de NFS-e de empresa existente;
- empresas novas entram ativas, sem o módulo NFS-e (liga-se depois, se for o caso).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .documentos import validar_cnpj


@dataclass
class Resultado:
    criadas: list[str] = field(default_factory=list)
    atualizadas: list[str] = field(default_factory=list)
    sem_mudanca: int = 0
    ignoradas: list[str] = field(default_factory=list)   # motivo legível
    excluidas_antes: int = 0                              # excluídas no sistema: não voltam

    @property
    def resumo(self) -> str:
        return (f"{len(self.criadas)} criada(s), {len(self.atualizadas)} atualizada(s), "
                f"{self.sem_mudanca} sem mudança, {len(self.ignoradas)} não importada(s)"
                + (f", {self.excluidas_antes} excluída(s) anteriormente (ignoradas)" if self.excluidas_antes else ""))


def _digitos(v) -> str:
    return re.sub(r"\D", "", str(v or ""))


def _codigo(v) -> str:
    """Código numérico do arquivador (24) -> '24'; códigos como 'MEI-…' não viram código interno."""
    s = str(v if v is not None else "").strip()
    return str(int(s)) if s.isdigit() else ""


def ler_arquivo(conteudo: bytes) -> list[dict]:
    try:
        dados = json.loads(conteudo.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ValueError("O arquivo não é um JSON válido (esperado: empresas.json do Bot Arquivador).") from e
    if isinstance(dados, dict) and isinstance(dados.get("empresas"), list):
        dados = dados["empresas"]
    if not isinstance(dados, list) or not all(isinstance(x, dict) for x in dados):
        raise ValueError("Formato inesperado: o empresas.json deve ser uma lista de empresas.")
    return dados


def importar(db: Session, registros: list[dict]) -> Resultado:
    from .configuracoes import ler_config
    res = Resultado()
    excluidas = set(filter(None, (ler_config(db, "empresas_excluidas") or "").split(",")))
    todas = db.scalars(select(models.Prestador)).all()
    por_cnpj: dict[str, models.Prestador] = {}
    por_codigo: dict[str, models.Prestador] = {}
    for p in todas:
        por_cnpj[p.cnpj] = p
        for c in p.lista_cnpjs_adicionais:
            por_cnpj.setdefault(c, p)
        if _codigo(p.codigo_interno):
            por_codigo[_codigo(p.codigo_interno)] = p

    for r in registros:
        nome = str(r.get("nome") or "").strip().upper()
        cod = _codigo(r.get("codigo"))
        rotulo = f"{r.get('codigo')} — {nome or '(sem nome)'}"
        cnpjs = list(dict.fromkeys(c for c in (_digitos(x) for x in r.get("cnpjs") or []) if c))
        cnpjs_validos = [c for c in cnpjs if len(c) == 14 and validar_cnpj(c)]
        if not cnpjs_validos:
            res.ignoradas.append(f"{rotulo}: sem CNPJ válido no cadastro do arquivador")
            continue
        achadas = {id(por_cnpj[c]): por_cnpj[c] for c in cnpjs_validos if c in por_cnpj}
        if not achadas and excluidas.intersection(cnpjs_validos):
            res.excluidas_antes += 1
            continue
        if len(achadas) > 1:
            res.ignoradas.append(f"{rotulo}: os CNPJs pertencem a empresas diferentes do sistema")
            continue
        p = next(iter(achadas.values()), None)
        if p is None and cod and cod in por_codigo:
            res.ignoradas.append(f"{rotulo}: o código {cod} já é de {por_codigo[cod].razao_social} "
                                 f"({por_codigo[cod].cnpj}) com outro CNPJ — conferir")
            continue
        if p is not None and cod and _codigo(p.codigo_interno) and _codigo(p.codigo_interno) != cod:
            res.ignoradas.append(f"{rotulo}: no sistema esta empresa tem o código {p.codigo_interno} — conferir")
            continue
        pasta = str(r.get("pasta") or "").strip()[:300]
        apelidos = [str(a).strip().upper() for a in r.get("aliases") or [] if str(a).strip()]
        if p is None:
            p = models.Prestador(
                cnpj=cnpjs_validos[0], razao_social=nome[:150] or cnpjs_validos[0], codigo_interno=cod,
                regime="MEI" if re.search(r"\\MEIS?\\", pasta, re.I) else "SIMPLES", modo="IMPORTACAO",
                ambiente="PRODUCAO", ativo=True, usa_nfse=False, usa_receita=True, usa_econtador=True, usa_cnd=True,
                importar_automaticamente=False, pasta_drive=pasta,
                cnpjs_adicionais=", ".join(cnpjs_validos[1:])[:300],
                apelidos="\n".join(a for a in dict.fromkeys(apelidos) if a != nome))
            db.add(p)
            db.flush()
            por_cnpj.update({c: p for c in cnpjs_validos})
            if cod:
                por_codigo[cod] = p
            res.criadas.append(rotulo)
            continue
        mudou = []
        if pasta and p.pasta_drive != pasta:
            p.pasta_drive = pasta
            mudou.append("pasta")
        if cod and not p.codigo_interno:
            p.codigo_interno = cod
            por_codigo[cod] = p
            mudou.append("código")
        extras = [c for c in cnpjs_validos if c != p.cnpj and c not in p.lista_cnpjs_adicionais]
        if extras:
            p.cnpjs_adicionais = ", ".join(p.lista_cnpjs_adicionais + extras)[:300]
            por_cnpj.update({c: p for c in extras})
            mudou.append("CNPJs")
        atuais = [a for a in (p.apelidos or "").splitlines() if a.strip()]
        novos = [a for a in dict.fromkeys(apelidos) if a not in atuais and a != p.razao_social]
        if novos:
            p.apelidos = "\n".join(atuais + novos)
            mudou.append("apelidos")
        if mudou:
            res.atualizadas.append(f"{rotulo} ({', '.join(mudou)})")
        else:
            res.sem_mudanca += 1
    return res
