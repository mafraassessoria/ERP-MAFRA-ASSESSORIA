"""Backup do Emissor NFS-e.

Gera um .zip com: banco de dados (cópia consistente, mesmo com o sistema rodando),
pasta de documentos (XML/PDF) e a chave mestra (sem ela os certificados não abrem).

Uso:   python -m scripts.backup
Agende 1x por dia (Agendador de Tarefas do Windows ou cron no Linux).
"""
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import (  # noqa: E402
    BACKUP_COPIA_DIR, BACKUP_DIR, BACKUPS_MANTIDOS, CHAVE_MESTRA_PATH, DATA_DIR, DB_URL, XML_DIR,
)


def _copiar_sqlite(destino: Path) -> None:
    origem = DB_URL.replace("sqlite:///", "", 1)
    src = sqlite3.connect(origem)
    dst = sqlite3.connect(destino)
    with dst:
        src.backup(dst)  # API de backup online do SQLite: cópia íntegra
    dst.close()
    src.close()
    con = sqlite3.connect(destino)
    ok = con.execute("PRAGMA integrity_check").fetchone()[0]
    con.close()
    if ok != "ok":
        raise RuntimeError(f"Cópia do banco falhou na verificação de integridade: {ok}")


def fazer_backup() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    carimbo = datetime.now().strftime("%Y%m%d_%H%M%S")
    arquivo = BACKUP_DIR / f"emissor_nfse_{carimbo}.zip"

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        with zipfile.ZipFile(arquivo, "w", zipfile.ZIP_DEFLATED) as z:
            if DB_URL.startswith("sqlite"):
                banco = tmp / "emissor.db"
                _copiar_sqlite(banco)
                z.write(banco, "emissor.db")
            else:
                z.writestr("LEIA-ME.txt", "Banco PostgreSQL: faça o backup com pg_dump (ver README).")
            if XML_DIR.exists():
                for f in XML_DIR.rglob("*"):
                    if f.is_file():
                        z.write(f, f"documentos/{f.relative_to(XML_DIR)}")
            z.writestr("info.txt", f"Backup gerado em {datetime.now():%d/%m/%Y %H:%M:%S}\nOrigem: {DATA_DIR}\n")

    # A chave mestra vai em arquivo SEPARADO, para poder ser guardada em outro lugar.
    chave_bkp = BACKUP_DIR / "chave_mestra.key"
    shutil.copy2(CHAVE_MESTRA_PATH, chave_bkp)

    # Mantém apenas os N backups mais recentes
    antigos = sorted(BACKUP_DIR.glob("emissor_nfse_*.zip"))[:-BACKUPS_MANTIDOS]
    for a in antigos:
        a.unlink()

    if BACKUP_COPIA_DIR:
        destino = Path(BACKUP_COPIA_DIR)
        destino.mkdir(parents=True, exist_ok=True)
        shutil.copy2(arquivo, destino / arquivo.name)
        for a in sorted(destino.glob("emissor_nfse_*.zip"))[:-BACKUPS_MANTIDOS]:
            a.unlink()

    return arquivo


if __name__ == "__main__":
    try:
        f = fazer_backup()
        print(f"Backup concluído: {f} ({f.stat().st_size / 1024:.0f} KB)")
    except Exception as e:  # noqa: BLE001
        print(f"ERRO NO BACKUP: {e}", file=sys.stderr)
        sys.exit(1)
