"""Entrega de arquivos ao Bot Arquivador (pasta de entrada). O arquivador lê o conteúdo, renomeia e guarda no Drive."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from sqlalchemy.orm import Session

from .configuracoes import ler_config

ENTRADA_PADRAO = r"C:\Monitor de Arquivos"


def entrada(db: Session) -> str:
    return ler_config(db, "arquivador_entrada") or ENTRADA_PADRAO


def entregar(db: Session, origem: Path, nome: str) -> str:
    """Copia para a entrada como .pending, confere o hash e só então dá o nome final. Devolve o caminho entregue."""
    origem = Path(origem)
    if not origem.is_file():
        raise ValueError("Arquivo de origem não encontrado")
    pasta = Path(entrada(db))
    if not pasta.is_dir():
        raise ValueError(f"Pasta de entrada do Bot Arquivador não encontrada: {pasta}")
    nome = "".join(ch for ch in nome if ch.isalnum() or ch in " -_.").strip() or origem.name
    alvo, temp = pasta / nome, pasta / (nome + ".pending")
    if alvo.exists():
        raise ValueError(f"Já existe {nome} na entrada do arquivador (pendente lá?)")
    dados = origem.read_bytes()
    with temp.open("xb") as f:
        f.write(dados)
    if hashlib.sha256(temp.read_bytes()).digest() != hashlib.sha256(dados).digest():
        temp.unlink(missing_ok=True)
        raise ValueError("Cópia para a entrada ficou diferente do original")
    os.replace(temp, alvo)
    return str(alvo)
