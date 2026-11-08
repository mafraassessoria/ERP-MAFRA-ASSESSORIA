"""Importa do ADN as notas de todas as empresas marcadas para importação automática.

Uso:   python -m scripts.importar
Executado pela tarefa agendada "Emissor NFS-e - Importacao ADN" (2x por dia).
"""
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import models  # noqa: E402
from app.db import SessionLocal, criar_tabelas  # noqa: E402
from app.importacao import importar_todos  # noqa: E402


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    criar_tabelas()
    db = SessionLocal()
    try:
        print(f"[{datetime.now():%d/%m/%Y %H:%M}] Importação ADN iniciada")
        resultados = importar_todos(db, "agendador")
        falhas = 0
        for p, r in resultados:
            print(f"  {p.razao_social} ({p.cnpj}): {r.mensagem}")
            falhas += 0 if r.ok else 1
        db.add(models.Auditoria(usuario_login="agendador", acao="IMPORTAR_ADN_AGENDADO", entidade="",
                                detalhes=f"{len(resultados)} empresas, {falhas} com problema", data_hora=datetime.now()))
        db.commit()
        print(f"  Concluído: {len(resultados)} empresa(s), {falhas} com problema.")
        return 1 if falhas else 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
