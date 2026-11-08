from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DB_URL

_sqlite = DB_URL.startswith("sqlite")
engine = create_engine(
    DB_URL,
    connect_args={"check_same_thread": False} if _sqlite else {},
    future=True,
)

if _sqlite:
    @event.listens_for(engine, "connect")
    def _pragmas(conn, _):
        cur = conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")  # mais seguro com vários usuários
        cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def criar_tabelas() -> None:
    from . import models  # noqa: F401
    Base.metadata.create_all(engine)
    migrar_colunas()
    remover_unico_chave_global()


def _literal(valor) -> str | None:
    from decimal import Decimal
    if isinstance(valor, bool):
        return ("1" if valor else "0") if _sqlite else ("TRUE" if valor else "FALSE")
    if isinstance(valor, (int, float, Decimal)):
        return str(valor)
    if isinstance(valor, str):
        return "'" + valor.replace("'", "''") + "'"
    return None


def migrar_colunas() -> None:
    """Atualização simples do banco: acrescenta colunas novas às tabelas existentes.

    Assim, uma versão nova do sistema roda sobre o banco antigo sem perder dados.
    (Só ACRESCENTA colunas; nunca apaga nem altera.)
    """
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    with engine.begin() as conn:
        for tabela in Base.metadata.sorted_tables:
            if not insp.has_table(tabela.name):
                continue
            existentes = {c["name"] for c in insp.get_columns(tabela.name)}
            for col in tabela.columns:
                if col.name in existentes:
                    continue
                tipo = col.type.compile(engine.dialect)
                sql = f'ALTER TABLE {tabela.name} ADD COLUMN {col.name} {tipo}'
                padrao = None
                if col.default is not None and getattr(col.default, "is_scalar", False):
                    padrao = _literal(col.default.arg)
                if padrao is not None:
                    sql += f" DEFAULT {padrao}"
                conn.execute(text(sql))


def remover_unico_chave_global() -> None:
    """Versões até a Etapa 2 exigiam chave de acesso única no banco todo. A partir da Etapa 3
    a mesma NFS-e pode aparecer para duas empresas do escritório (uma prestou, a outra tomou),
    então a unicidade passa a ser (empresa, chave)."""
    from sqlalchemy import text

    with engine.begin() as conn:
        if not _sqlite:
            conn.execute(text("ALTER TABLE notas_fiscais DROP CONSTRAINT IF EXISTS notas_fiscais_chave_acesso_key"))
            return
        precisa = False
        for idx in conn.execute(text("PRAGMA index_list('notas_fiscais')")).mappings():
            if idx["unique"]:
                cols = [r["name"] for r in conn.execute(text(f"PRAGMA index_info('{idx['name']}')")).mappings()]
                if cols == ["chave_acesso"]:
                    precisa = True
        if not precisa:
            return
    # Reconstrói a tabela (o SQLite não permite remover essa restrição de outro jeito)
    from . import models
    tabela = models.NotaFiscal.__table__
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute("PRAGMA foreign_keys=OFF")
        cur.execute("BEGIN")
        cur.execute("ALTER TABLE notas_fiscais RENAME TO notas_fiscais_antiga")
        for idx in list(cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='notas_fiscais_antiga' AND sql IS NOT NULL")):
            cur.execute(f'DROP INDEX "{idx[0]}"')
        raw.commit()
    finally:
        raw.close()
    tabela.create(engine)
    with engine.begin() as conn:
        antigas = {r["name"] for r in conn.execute(text("PRAGMA table_info('notas_fiscais_antiga')")).mappings()}
        comuns = ", ".join(c.name for c in tabela.columns if c.name in antigas)
        conn.execute(text(f"INSERT INTO notas_fiscais ({comuns}) SELECT {comuns} FROM notas_fiscais_antiga"))
        conn.execute(text("DROP TABLE notas_fiscais_antiga"))
