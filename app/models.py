"""Modelo de dados multiempresa.

Prestador = empresa cliente da contabilidade (quem presta o serviço).
Tomador   = cliente do prestador (quem recebe a nota).
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean, Date, DateTime, ForeignKey, Integer, LargeBinary, Numeric, String,
    Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

# ---- Listas de opções -------------------------------------------------------

REGIMES = {
    "MEI": "MEI",
    "SIMPLES": "Simples Nacional (ME/EPP)",
    "SIMPLES_EXCESSO": "Simples Nacional – excesso de sublimite",
    "REGULAR": "Regime Regular (Lucro Presumido/Real)",
}

REGIMES_CURTO = {"MEI": "MEI", "SIMPLES": "Simples", "SIMPLES_EXCESSO": "Simples (excesso)", "REGULAR": "Regime Regular"}

MODOS = {
    "EMISSAO": "Emissão pelo escritório",
    "IMPORTACAO": "Somente importação (empresa emite as próprias notas)",
}

AMBIENTES = {"HOMOLOGACAO": "Homologação (teste)", "PRODUCAO": "Produção"}

PERFIS = {"ADMIN": "Administrador", "OPERADOR": "Operador"}

ORIGENS_NOTA = {"EMITIDA": "Emitida pelo sistema", "IMPORTADA": "Importada do ADN"}
TRIBUTACAO_ISSQN = {1: "Operação tributável", 2: "Imunidade", 3: "Exportação de serviço", 4: "Não incidência"}
RETENCAO_ISS = {1: "Não retido", 2: "Retido pelo tomador", 3: "Retido pelo intermediário"}
TIPOS_IMUNIDADE = {
    0: "Imunidade (tipo não informado)",
    1: "Patrimônio, renda ou serviços, uns dos outros (CF88, art. 150, VI, a)",
    2: "Templos de qualquer culto (CF88, art. 150, VI, b)",
    3: "Partidos políticos, sindicatos de trabalhadores, educação e assistência social sem fins lucrativos (CF88, art. 150, VI, c)",
    4: "Livros, jornais, periódicos e o papel destinado a sua impressão (CF88, art. 150, VI, d)",
    5: "Fonogramas e videofonogramas musicais produzidos no Brasil (CF88, art. 150, VI, e)",
}
TIPOS_RET_PISCOFINS = {
    "": "Não informar",
    "0": "PIS/COFINS/CSLL não retidos",
    "1": "PIS/COFINS retidos",
    "2": "PIS/COFINS não retidos",
    "3": "PIS/COFINS/CSLL retidos",
    "4": "PIS/COFINS retidos, CSLL não retida",
    "5": "PIS retido, COFINS/CSLL não retidos",
    "6": "COFINS retida, PIS/CSLL não retidos",
    "7": "PIS não retido, COFINS/CSLL retidos",
    "8": "PIS/COFINS não retidos, CSLL retida",
    "9": "COFINS não retida, PIS/CSLL retidos",
}
TOT_TRIB_TIPOS = {"SN": "Alíquota do Simples Nacional", "VALOR": "Valores monetários (R$)", "PERCENTUAL": "Valores percentuais (%)"}
# Subitens da LC 116 (4 primeiros dígitos do cTribNac) que exigem a identificação da obra.
# Pode ser alterado em Configurações.
OBRA_SUBITENS_PADRAO = "0702,0704,0705,0719"
REG_AP_TRIB_SN = {
    1: "Tributos federais e ISS pelo Simples",
    2: "Federais pelo Simples; ISS fora do Simples",
    3: "Federais e ISS fora do Simples",
}
MOTIVOS_CANCELAMENTO = {1: "Erro na emissão", 2: "Serviço não prestado", 9: "Outros"}

SITUACOES_NOTA = {
    "RASCUNHO": "Rascunho",
    "PROCESSANDO": "Enviada / aguardando",
    "AUTORIZADA": "Autorizada",
    "REJEITADA": "Rejeitada",
    "CANCELADA": "Cancelada",
}


class Timestamps:
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    atualizado_em: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now
    )


# ---- Usuários e auditoria ---------------------------------------------------

class Usuario(Timestamps, Base):
    __tablename__ = "usuarios"
    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(120))
    login: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    senha_hash: Mapped[str] = mapped_column(String(255))
    perfil: Mapped[str] = mapped_column(String(20), default="OPERADOR")
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    totp_segredo: Mapped[str] = mapped_column(Text, default="")      # criptografado; vazio = sem verificação em duas etapas
    totp_ultimo: Mapped[int] = mapped_column(Integer, default=0)     # último passo aceito (impede reusar o mesmo código)
    email_2fa: Mapped[str] = mapped_column(String(200), default="")   # e-mail para o código alternativo (sem o celular)
    email_codigo_hash: Mapped[str] = mapped_column(String(255), default="")
    email_codigo_expira: Mapped[datetime | None] = mapped_column(DateTime)
    email_codigo_tentativas: Mapped[int] = mapped_column(Integer, default=0)
    email_codigo_enviado: Mapped[datetime | None] = mapped_column(DateTime)

    @property
    def tem_2fa(self) -> bool:
        return bool(self.totp_segredo)


class Auditoria(Base):
    __tablename__ = "auditoria"
    id: Mapped[int] = mapped_column(primary_key=True)
    data_hora: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    usuario_id: Mapped[int | None] = mapped_column(ForeignKey("usuarios.id"))
    usuario_login: Mapped[str] = mapped_column(String(60), default="")
    acao: Mapped[str] = mapped_column(String(60))
    entidade: Mapped[str] = mapped_column(String(60), default="")
    entidade_id: Mapped[int | None] = mapped_column(Integer)
    detalhes: Mapped[str] = mapped_column(Text, default="")
    ip: Mapped[str] = mapped_column(String(64), default="")


# ---- Prestadores (clientes da contabilidade) --------------------------------

def ordem_prestador_sql():
    """ORDER BY pelo código interno numérico crescente (como no e-Contador); sem código vai para o fim, por nome."""
    from sqlalchemy import case, func
    c = Prestador.codigo_interno
    return (case((func.coalesce(c, "") == "", 1), else_=0), func.length(c), c, Prestador.razao_social)


class Prestador(Timestamps, Base):
    __tablename__ = "prestadores"

    @property
    def codigo_fmt(self) -> str:
        """Código interno no formato do e-Contador: 1 -> 00001."""
        c = (self.codigo_interno or "").strip()
        return c.zfill(5) if c.isdigit() else c

    @property
    def nome_exibicao(self) -> str:
        return f"{self.codigo_fmt} · {self.razao_social}" if self.codigo_fmt else self.razao_social

    @property
    def ordem(self) -> tuple:
        c = (self.codigo_interno or "").strip()
        return (0, int(c), "") if c.isdigit() else (1, 0, self.razao_social or "")
    id: Mapped[int] = mapped_column(primary_key=True)
    codigo_interno: Mapped[str] = mapped_column(String(20), default="")  # nº da empresa no escritório
    cnpj: Mapped[str] = mapped_column(String(14), unique=True, index=True)
    razao_social: Mapped[str] = mapped_column(String(150))
    nome_fantasia: Mapped[str] = mapped_column(String(150), default="")
    inscricao_municipal: Mapped[str] = mapped_column(String(20), default="")
    regime: Mapped[str] = mapped_column(String(20), default="SIMPLES")
    modo: Mapped[str] = mapped_column(String(20), default="EMISSAO")
    ambiente: Mapped[str] = mapped_column(String(20), default="HOMOLOGACAO")

    # Endereço
    cep: Mapped[str] = mapped_column(String(8), default="")
    logradouro: Mapped[str] = mapped_column(String(150), default="")
    numero: Mapped[str] = mapped_column(String(20), default="")
    complemento: Mapped[str] = mapped_column(String(60), default="")
    bairro: Mapped[str] = mapped_column(String(60), default="")
    codigo_municipio_ibge: Mapped[str] = mapped_column(String(7), default="")
    municipio: Mapped[str] = mapped_column(String(80), default="")
    uf: Mapped[str] = mapped_column(String(2), default="")

    email: Mapped[str] = mapped_column(String(120), default="")
    telefone: Mapped[str] = mapped_column(String(20), default="")

    # Tributação na DPS
    # regApTribSN: 1 = federais e ISS pelo Simples; 2 = federais pelo SN e ISS fora; 3 = ambos fora
    reg_ap_trib_sn: Mapped[int] = mapped_column(Integer, default=1)
    reg_esp_trib: Mapped[int] = mapped_column(Integer, default=0)  # regime especial (0 = nenhum)
    # % aproximado total de tributos do Simples (Lei da Transparência) — obrigatório p/ ME/EPP
    p_tot_trib_sn: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=0)
    cst_piscofins: Mapped[str] = mapped_column(String(2), default="08")

    # Numeração da DPS (usada só no modo EMISSAO) — um contador por ambiente
    serie_dps: Mapped[str] = mapped_column(String(5), default="00001")
    proximo_numero_dps: Mapped[int] = mapped_column(Integer, default=1)  # produção
    proximo_numero_dps_homologacao: Mapped[int] = mapped_column(Integer, default=1)

    # Município aderente ao Emissor Nacional? (conferir antes de emitir)
    municipio_aderente: Mapped[bool] = mapped_column(Boolean, default=True)
    # Autorização formal do cliente para usar o certificado
    autorizacao_uso_certificado: Mapped[bool] = mapped_column(Boolean, default=False)
    # Importação pelo ADN usando o certificado do ESCRITÓRIO (procuração eletrônica do cliente)
    importar_por_procuracao: Mapped[bool] = mapped_column(Boolean, default=False)
    importar_automaticamente: Mapped[bool] = mapped_column(Boolean, default=True)
    # Envia PDF + XML ao tomador assim que a nota for autorizada
    enviar_email_automatico: Mapped[bool] = mapped_column(Boolean, default=False)
    # Emissão em lote (planilha): só para as empresas que usam
    usa_lote: Mapped[bool] = mapped_column(Boolean, default=False)
    # Último pedido de renovação/emissão de certificado enviado à empresa (vencido ou sem certificado)
    certificado_pedido_em: Mapped[datetime | None] = mapped_column(DateTime)

    observacoes: Mapped[str] = mapped_column(Text, default="")
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)

    # ---- Cadastro geral: o que o escritório usa para esta empresa (módulos) ----
    usa_nfse: Mapped[bool] = mapped_column(Boolean, default=True)        # emissor/importação de NFS-e
    usa_receita: Mapped[bool] = mapped_column(Boolean, default=True)     # Integra Contador (e-CAC)
    usa_econtador: Mapped[bool] = mapped_column(Boolean, default=True)   # integração com o e-Contador
    usa_cnd: Mapped[bool] = mapped_column(Boolean, default=True)         # certidões (Pack CND)
    id_econtador: Mapped[str] = mapped_column(String(30), default="")    # id da empresa na API do e-Contador
    pasta_drive: Mapped[str] = mapped_column(String(300), default="")    # pasta da empresa no Drive (arquivador)
    cnpjs_adicionais: Mapped[str] = mapped_column(String(300), default="")  # outros CNPJs/CPFs da empresa, separados por vírgula
    apelidos: Mapped[str] = mapped_column(Text, default="")              # outros nomes, um por linha (arquivador)

    certificados: Mapped[list["Certificado"]] = relationship(
        back_populates="prestador", cascade="all, delete-orphan", order_by="Certificado.validade_fim.desc()"
    )
    tomadores: Mapped[list["Tomador"]] = relationship(back_populates="prestador", cascade="all, delete-orphan")
    servicos: Mapped[list["ServicoPadrao"]] = relationship(back_populates="prestador", cascade="all, delete-orphan")

    @property
    def emite_nfse(self) -> bool:
        return bool(self.usa_nfse) and self.modo == "EMISSAO"

    @property
    def modulos(self) -> list[str]:
        return [n for n, ok in (("NFS-e", self.usa_nfse), ("Receita", self.usa_receita),
                                ("e-Contador", self.usa_econtador), ("CND", self.usa_cnd)) if ok]

    @property
    def lista_cnpjs_adicionais(self) -> list[str]:
        import re
        return [c for c in (re.sub(r"\D", "", x) for x in (self.cnpjs_adicionais or "").split(",")) if c]

    @property
    def certificado_ativo(self) -> "Certificado | None":
        for c in self.certificados:
            if c.ativo:
                return c
        return None

    @property
    def certificado_valido(self) -> "Certificado | None":
        """Certificado da própria empresa ativo e dentro da validade."""
        c = self.certificado_ativo
        return c if c is not None and c.validade_fim >= datetime.now() else None

    @property
    def usa_procuracao(self) -> bool:
        """Automático: sem certificado próprio válido, usa o do escritório (procuração)."""
        return self.certificado_valido is None


class Certificado(Base):
    """Certificado A1 (.pfx) guardado CRIPTOGRAFADO com a chave mestra."""
    __tablename__ = "certificados"
    id: Mapped[int] = mapped_column(primary_key=True)
    prestador_id: Mapped[int] = mapped_column(ForeignKey("prestadores.id", ondelete="CASCADE"), index=True)
    arquivo_cripto: Mapped[bytes] = mapped_column(LargeBinary)
    senha_cripto: Mapped[bytes] = mapped_column(LargeBinary)
    nome_arquivo: Mapped[str] = mapped_column(String(200), default="")
    titular: Mapped[str] = mapped_column(String(200), default="")
    cnpj_certificado: Mapped[str] = mapped_column(String(14), default="")
    numero_serie: Mapped[str] = mapped_column(String(64), default="")
    emissora: Mapped[str] = mapped_column(String(200), default="")
    validade_inicio: Mapped[datetime] = mapped_column(DateTime)
    validade_fim: Mapped[datetime] = mapped_column(DateTime, index=True)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    enviado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    enviado_por: Mapped[str] = mapped_column(String(60), default="")
    # Avisos de vencimento já enviados por e-mail (dias, ex.: "15,12,9")
    avisos_enviados: Mapped[str] = mapped_column(String(60), default="")
    # Pedido de renovação enviado à empresa depois de vencido
    renovacao_pedida_em: Mapped[datetime | None] = mapped_column(DateTime)

    prestador: Mapped[Prestador] = relationship(back_populates="certificados")

    @property
    def dias_para_vencer(self) -> int:
        return (self.validade_fim.date() - date.today()).days


class ServicoPadrao(Timestamps, Base):
    """Serviços que o prestador costuma faturar — agiliza a emissão."""
    __tablename__ = "servicos_padrao"
    id: Mapped[int] = mapped_column(primary_key=True)
    prestador_id: Mapped[int] = mapped_column(ForeignKey("prestadores.id", ondelete="CASCADE"), index=True)
    apelido: Mapped[str] = mapped_column(String(80))
    descricao: Mapped[str] = mapped_column(Text)
    codigo_tributacao_nacional: Mapped[str] = mapped_column(String(6))  # cTribNac (item LC 116 + subitem + desdobro)
    codigo_tributacao_municipal: Mapped[str] = mapped_column(String(3), default="")
    codigo_nbs: Mapped[str] = mapped_column(String(9), default="")
    aliquota_iss: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=0)  # informativa
    valor_padrao: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)
    # tribISSQN: 1 = tributável; 2 = imunidade; 3 = exportação; 4 = não incidência
    tributacao_issqn: Mapped[int] = mapped_column(Integer, default=1)
    # tpRetISSQN: 1 = não retido; 2 = retido pelo tomador; 3 = retido pelo intermediário
    tipo_retencao_iss: Mapped[int] = mapped_column(Integer, default=1)
    municipio_prestacao: Mapped[str] = mapped_column(String(7), default="")  # vazio = do prestador
    info_complementares: Mapped[str] = mapped_column(Text, default="")
    obra_id: Mapped[int | None] = mapped_column(ForeignKey("obras.id", ondelete="SET NULL"))
    ded_tipo: Mapped[str] = mapped_column(String(10), default="")  # "" | PERCENTUAL | VALOR
    ded_valor: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)
    aliquota_iss: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=0)  # pAliq (enviada se ISS retido)
    ret_inss: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)    # vRetCP
    ret_irrf: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)    # vRetIRRF
    ret_csll: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)    # vRetCSLL
    # Serviço de obra: a nota exige identificação da obra (CNO, CIB ou endereço)
    exige_obra: Mapped[bool] = mapped_column(Boolean, default=False)
    # Retenções sugeridas (% sobre o valor do serviço) — o usuário confirma na nota
    perc_ret_inss: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=0)
    perc_ret_irrf: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=0)
    perc_ret_csll: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=0)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)

    prestador: Mapped[Prestador] = relationship(back_populates="servicos")


class CodigoTributacao(Base):
    """Tabela nacional de códigos de tributação (lista da LC 116 com desdobro nacional)."""
    __tablename__ = "tab_ctribnac"
    codigo: Mapped[str] = mapped_column(String(6), primary_key=True)
    descricao: Mapped[str] = mapped_column(Text, default="")
    oficial: Mapped[bool] = mapped_column(Boolean, default=False)  # veio da planilha oficial


class CodigoNBS(Base):
    """Nomenclatura Brasileira de Serviços (9 dígitos)."""
    __tablename__ = "tab_nbs"
    codigo: Mapped[str] = mapped_column(String(9), primary_key=True)
    descricao: Mapped[str] = mapped_column(Text, default="")
    oficial: Mapped[bool] = mapped_column(Boolean, default=False)


class CorrelacaoNBS(Base):
    """Correlação subitem da LC 116 (4 dígitos) × NBS (Anexo VIII)."""
    __tablename__ = "tab_correlacao_nbs"
    id: Mapped[int] = mapped_column(primary_key=True)
    subitem: Mapped[str] = mapped_column(String(6), index=True)
    nbs: Mapped[str] = mapped_column(String(9), index=True)


class CodigoEmpresa(Base):
    """Códigos que a empresa usa (cTribNac e NBS) — só eles aparecem na emissão."""
    __tablename__ = "codigos_empresa"
    __table_args__ = (UniqueConstraint("prestador_id", "tipo", "codigo", name="uq_codigo_empresa"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    prestador_id: Mapped[int] = mapped_column(ForeignKey("prestadores.id", ondelete="CASCADE"), index=True)
    tipo: Mapped[str] = mapped_column(String(10))  # CTRIBNAC | NBS
    codigo: Mapped[str] = mapped_column(String(9))


class Intermediario(Timestamps, Base):
    """Intermediário da operação (quem intermediou e, se for o caso, reteve o ISS)."""
    __tablename__ = "intermediarios"
    __table_args__ = (UniqueConstraint("prestador_id", "documento", name="uq_interm_prestador_doc"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    prestador_id: Mapped[int] = mapped_column(ForeignKey("prestadores.id", ondelete="CASCADE"), index=True)
    documento: Mapped[str] = mapped_column(String(14), index=True)
    nome: Mapped[str] = mapped_column(String(150))
    inscricao_municipal: Mapped[str] = mapped_column(String(20), default="")
    email: Mapped[str] = mapped_column(String(120), default="")
    telefone: Mapped[str] = mapped_column(String(20), default="")
    cep: Mapped[str] = mapped_column(String(8), default="")
    logradouro: Mapped[str] = mapped_column(String(150), default="")
    numero: Mapped[str] = mapped_column(String(20), default="")
    complemento: Mapped[str] = mapped_column(String(60), default="")
    bairro: Mapped[str] = mapped_column(String(60), default="")
    codigo_municipio_ibge: Mapped[str] = mapped_column(String(7), default="")
    municipio: Mapped[str] = mapped_column(String(80), default="")
    uf: Mapped[str] = mapped_column(String(2), default="")
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)


class Tomador(Timestamps, Base):
    __tablename__ = "tomadores"
    __table_args__ = (UniqueConstraint("prestador_id", "documento", name="uq_tomador_prestador_doc"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    prestador_id: Mapped[int] = mapped_column(ForeignKey("prestadores.id", ondelete="CASCADE"), index=True)
    documento: Mapped[str] = mapped_column(String(14), index=True)  # CPF ou CNPJ (sem máscara)
    nome: Mapped[str] = mapped_column(String(150))
    inscricao_municipal: Mapped[str] = mapped_column(String(20), default="")
    email: Mapped[str] = mapped_column(String(120), default="")
    telefone: Mapped[str] = mapped_column(String(20), default="")
    cep: Mapped[str] = mapped_column(String(8), default="")
    logradouro: Mapped[str] = mapped_column(String(150), default="")
    numero: Mapped[str] = mapped_column(String(20), default="")
    complemento: Mapped[str] = mapped_column(String(60), default="")
    bairro: Mapped[str] = mapped_column(String(60), default="")
    codigo_municipio_ibge: Mapped[str] = mapped_column(String(7), default="")
    municipio: Mapped[str] = mapped_column(String(80), default="")
    uf: Mapped[str] = mapped_column(String(2), default="")
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    data_nascimento: Mapped[date | None] = mapped_column(Date, nullable=True)   # pessoa física (Consulta CPF v3)

    prestador: Mapped[Prestador] = relationship(back_populates="tomadores")

    @property
    def tipo_documento(self) -> str:
        return "CPF" if len(self.documento) == 11 else "CNPJ"


# ---- Notas (emitidas e importadas) — usadas nas próximas etapas ------------

class NotaFiscal(Timestamps, Base):
    __tablename__ = "notas_fiscais"
    __table_args__ = (
        UniqueConstraint("prestador_id", "ambiente", "serie_dps", "numero_dps", name="uq_nota_dps"),
        UniqueConstraint("prestador_id", "chave_acesso", name="uq_nota_prestador_chave"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    prestador_id: Mapped[int] = mapped_column(ForeignKey("prestadores.id"), index=True)
    tomador_id: Mapped[int | None] = mapped_column(ForeignKey("tomadores.id"))
    origem: Mapped[str] = mapped_column(String(20), default="EMITIDA")
    situacao: Mapped[str] = mapped_column(String(20), default="RASCUNHO", index=True)
    ambiente: Mapped[str] = mapped_column(String(20), default="HOMOLOGACAO")

    serie_dps: Mapped[str | None] = mapped_column(String(5))
    numero_dps: Mapped[int | None] = mapped_column(Integer)
    id_dps: Mapped[str | None] = mapped_column(String(50), index=True)
    chave_acesso: Mapped[str | None] = mapped_column(String(60), index=True)
    # PRESTADO = a empresa (prestador_id) prestou o serviço; TOMADO = a empresa contratou o serviço
    papel: Mapped[str] = mapped_column(String(10), default="PRESTADO", index=True)
    emitente_documento: Mapped[str] = mapped_column(String(14), default="")
    emitente_nome: Mapped[str] = mapped_column(String(150), default="")
    numero_nfse: Mapped[str | None] = mapped_column(String(20))
    nsu: Mapped[int | None] = mapped_column(Integer)

    competencia: Mapped[date | None] = mapped_column(Date, index=True)
    data_emissao: Mapped[datetime | None] = mapped_column(DateTime)
    tomador_documento: Mapped[str] = mapped_column(String(14), default="")
    tomador_nome: Mapped[str] = mapped_column(String(150), default="")
    servico_id: Mapped[int | None] = mapped_column(ForeignKey("servicos_padrao.id", ondelete="SET NULL"))
    descricao_servico: Mapped[str] = mapped_column(Text, default="")
    codigo_tributacao_nacional: Mapped[str] = mapped_column(String(6), default="")
    codigo_tributacao_municipal: Mapped[str] = mapped_column(String(3), default="")
    codigo_nbs: Mapped[str] = mapped_column(String(9), default="")
    municipio_prestacao: Mapped[str] = mapped_column(String(7), default="")
    tributacao_issqn: Mapped[int] = mapped_column(Integer, default=1)
    tipo_retencao_iss: Mapped[int] = mapped_column(Integer, default=1)
    info_complementares: Mapped[str] = mapped_column(Text, default="")
    obra_id: Mapped[int | None] = mapped_column(ForeignKey("obras.id", ondelete="SET NULL"))
    ded_tipo: Mapped[str] = mapped_column(String(10), default="")  # "" | PERCENTUAL | VALOR
    ded_valor: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)
    aliquota_iss: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=0)  # pAliq (enviada se ISS retido)
    ret_inss: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)    # vRetCP
    ret_irrf: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)    # vRetIRRF
    ret_csll: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)    # vRetCSLL
    intermediario_id: Mapped[int | None] = mapped_column(ForeignKey("intermediarios.id", ondelete="SET NULL"))
    valor_recebido_intermediario: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)  # vReceb
    desconto_incondicionado: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)
    desconto_condicionado: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)
    tipo_imunidade: Mapped[int | None] = mapped_column(Integer)
    tipo_ret_piscofins: Mapped[str] = mapped_column(String(1), default="")
    tot_trib_tipo: Mapped[str] = mapped_column(String(12), default="SN")  # SN | VALOR | PERCENTUAL
    tot_trib_fed: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)
    tot_trib_est: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)
    tot_trib_mun: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)
    lote: Mapped[str] = mapped_column(String(40), default="", index=True)  # emissão em lote por planilha
    valor_servicos: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)
    valor_iss: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)

    caminho_xml: Mapped[str] = mapped_column(String(300), default="")
    caminho_pdf: Mapped[str] = mapped_column(String(300), default="")
    caminho_evento_xml: Mapped[str] = mapped_column(String(300), default="")
    mensagem_retorno: Mapped[str] = mapped_column(Text, default="")
    criada_por: Mapped[str] = mapped_column(String(60), default="")
    emitida_por: Mapped[str] = mapped_column(String(60), default="")
    email_enviado_em: Mapped[datetime | None] = mapped_column(DateTime)
    email_enviado_para: Mapped[str] = mapped_column(String(300), default="")
    cancelada_em: Mapped[datetime | None] = mapped_column(DateTime)
    cancelada_por: Mapped[str] = mapped_column(String(60), default="")
    # Envio ao NF-Stock (API MS-Importação): "" pendente | PROCESSANDO | ENVIADO | ERRO
    nfstock_status: Mapped[str] = mapped_column(String(12), default="", index=True)
    nfstock_id: Mapped[str] = mapped_column(String(80), default="")
    nfstock_em: Mapped[datetime | None] = mapped_column(DateTime)
    nfstock_log: Mapped[str] = mapped_column(String(500), default="")
    nfstock_evento_id: Mapped[str] = mapped_column(String(80), default="")
    nfstock_evento_status: Mapped[str] = mapped_column(String(12), default="")
    motivo_cancelamento: Mapped[str] = mapped_column(Text, default="")

    prestador: Mapped[Prestador] = relationship()
    tomador: Mapped["Tomador"] = relationship()
    obra: Mapped["Obra"] = relationship()
    intermediario: Mapped["Intermediario"] = relationship()

    @property
    def iss_retido(self) -> Decimal:
        if (self.tipo_retencao_iss or 1) not in (2, 3):
            return Decimal("0")
        if self.valor_iss:
            return Decimal(self.valor_iss)
        # Rascunho: estimativa pela alíquota informada (a Sefin devolve o valor oficial)
        bc = Decimal(self.valor_servicos or 0) - Decimal(self.desconto_incondicionado or 0)
        if self.ded_tipo == "PERCENTUAL":
            bc -= bc * Decimal(self.ded_valor or 0) / 100
        elif self.ded_tipo == "VALOR":
            bc -= Decimal(self.ded_valor or 0)
        return (max(bc, Decimal("0")) * Decimal(self.aliquota_iss or 0) / 100).quantize(Decimal("0.01"))

    @property
    def valor_liquido(self) -> Decimal:
        """Serviço − descontos − ISS retido − retenções federais (IRRF, CSLL, contribuição previdenciária)."""
        v = Decimal(self.valor_servicos or 0)
        v -= Decimal(self.desconto_incondicionado or 0) + Decimal(self.desconto_condicionado or 0)
        v -= self.iss_retido
        v -= Decimal(self.ret_irrf or 0) + Decimal(self.ret_csll or 0) + Decimal(self.ret_inss or 0)
        return v

    @property
    def nome_prestador(self) -> str:
        return self.emitente_nome or self.prestador.razao_social
    servico: Mapped["ServicoPadrao"] = relationship()


class Obra(Timestamps, Base):
    """Obra de construção civil, vinculada a um tomador do prestador."""
    __tablename__ = "obras"
    id: Mapped[int] = mapped_column(primary_key=True)
    prestador_id: Mapped[int] = mapped_column(ForeignKey("prestadores.id", ondelete="CASCADE"), index=True)
    tomador_id: Mapped[int | None] = mapped_column(ForeignKey("tomadores.id", ondelete="CASCADE"), index=True)
    descricao: Mapped[str] = mapped_column(String(120), default="")      # apelido para escolher na nota
    cno: Mapped[str] = mapped_column(String(30), default="")             # cObra: CNO (ou CEI antigo)
    cib: Mapped[str] = mapped_column(String(8), default="")              # cCIB
    inscricao_imobiliaria: Mapped[str] = mapped_column(String(30), default="")
    cep: Mapped[str] = mapped_column(String(8), default="")
    logradouro: Mapped[str] = mapped_column(String(150), default="")
    numero: Mapped[str] = mapped_column(String(20), default="")
    complemento: Mapped[str] = mapped_column(String(60), default="")
    bairro: Mapped[str] = mapped_column(String(60), default="")
    codigo_municipio_ibge: Mapped[str] = mapped_column(String(7), default="")
    municipio: Mapped[str] = mapped_column(String(80), default="")
    uf: Mapped[str] = mapped_column(String(2), default="")
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)

    tomador: Mapped["Tomador"] = relationship()

    @property
    def identificacao(self) -> str:
        if self.cno:
            return f"CNO {self.cno}"
        if self.cib:
            return f"CIB {self.cib}"
        return f"{self.logradouro}, {self.numero} - {self.municipio}/{self.uf}".strip(" ,-/")

    @property
    def rotulo(self) -> str:
        return (self.descricao + " · " if self.descricao else "") + self.identificacao


class ConsultaReceita(Base):
    """Cada chamada ao Integra Contador (Serpro): guarda pedido, resposta, arquivos e custo estimado."""
    __tablename__ = "consultas_receita"
    id: Mapped[int] = mapped_column(primary_key=True)
    prestador_id: Mapped[int] = mapped_column(ForeignKey("prestadores.id", ondelete="CASCADE"), index=True)
    servico: Mapped[str] = mapped_column(String(40), index=True)      # código do catálogo
    id_servico: Mapped[str] = mapped_column(String(40), default="")
    tipo: Mapped[str] = mapped_column(String(12), default="")         # Consultar/Emitir/...
    parametros: Mapped[str] = mapped_column(Text, default="")         # JSON dos campos da tela
    chave_repeticao: Mapped[str] = mapped_column(String(200), default="", index=True)
    modo: Mapped[str] = mapped_column(String(15), default="")         # demonstracao | producao
    data_hora: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    usuario: Mapped[str] = mapped_column(String(40), default="")
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    http: Mapped[int] = mapped_column(Integer, default=0)
    mensagens: Mapped[str] = mapped_column(Text, default="")
    dados: Mapped[str] = mapped_column(Text, default="")              # JSON devolvido (sem os PDFs)
    arquivos: Mapped[str] = mapped_column(Text, default="")           # caminhos relativos, separados por "\n"
    chamadas: Mapped[int] = mapped_column(Integer, default=1)
    custo: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    lote: Mapped[str] = mapped_column(String(40), default="", index=True)

    prestador: Mapped["Prestador"] = relationship()

    @property
    def lista_arquivos(self) -> list[str]:
        return [a for a in (self.arquivos or "").split("\n") if a]


class Configuracao(Base):
    """Configurações gerais do escritório (chave/valor)."""
    __tablename__ = "configuracoes"
    chave: Mapped[str] = mapped_column(String(60), primary_key=True)
    valor: Mapped[str] = mapped_column(Text, default="")


PAPEIS_NOTA = {"PRESTADO": "Prestado (nota emitida pela empresa)", "TOMADO": "Tomado (nota recebida)"}


class ControleNSU(Base):
    """Último NSU baixado do ADN por prestador (importação das notas)."""
    __tablename__ = "controle_nsu"
    id: Mapped[int] = mapped_column(primary_key=True)
    prestador_id: Mapped[int] = mapped_column(ForeignKey("prestadores.id", ondelete="CASCADE"), unique=True)
    ambiente: Mapped[str] = mapped_column(String(20), default="PRODUCAO")
    ultimo_nsu: Mapped[int] = mapped_column(Integer, default=0)
    ultima_consulta: Mapped[datetime | None] = mapped_column(DateTime)
    ultimo_resultado: Mapped[str] = mapped_column(Text, default="")


class Certidao(Base):
    """Certidão vista no Pack CND (e-Contador). O PDF fica em dados/certidoes; a entrega ao Drive é feita pelo Bot Arquivador."""
    __tablename__ = "certidoes"
    id: Mapped[int] = mapped_column(primary_key=True)
    prestador_id: Mapped[int] = mapped_column(ForeignKey("prestadores.id", ondelete="CASCADE"), index=True)
    pack_id: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    tipo_id: Mapped[str] = mapped_column(String(20), default="")
    tipo_nome: Mapped[str] = mapped_column(String(200), default="")
    tipo: Mapped[str] = mapped_column(String(40), default="")            # chave conhecida (FGTS, CNDT…); vazio = revisar
    emissao: Mapped[date | None] = mapped_column(Date, nullable=True)
    vencimento: Mapped[date | None] = mapped_column(Date, nullable=True)
    situacao: Mapped[str] = mapped_column(String(60), default="")
    disponivel: Mapped[bool] = mapped_column(Boolean, default=False)
    avisos: Mapped[str] = mapped_column(String(200), default="")
    no_pack: Mapped[bool] = mapped_column(Boolean, default=True)          # apareceu na última consulta
    consultado_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    alterado_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    arquivo: Mapped[str] = mapped_column(String(300), default="")         # relativo a DATA_DIR
    sha256: Mapped[str] = mapped_column(String(64), default="")
    publicado_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    publicado_como: Mapped[str] = mapped_column(String(300), default="")
    erro: Mapped[str] = mapped_column(Text, default="")
    substituida_em: Mapped[date | None] = mapped_column(Date, nullable=True)      # histórico: quando saiu a mais nova
    substituida_por: Mapped[str] = mapped_column(String(20), default="")          # pack_id da certidão que a substituiu

    prestador: Mapped["Prestador"] = relationship()


class MonitorItem(Base):
    """Último resultado conhecido de cada acompanhamento do e-CAC por empresa (substitui o eRadar).

    item: SITFIS | PARCELAMENTO | CAIXA | PGDASD | DCTFWEB | MIT | DEFIS
    referencia: '2026-10' (mensal), '2025' (anual) ou '' (situação atual)
    status: REGULAR | PENDENCIA | ENTREGUE | NAO_ENTREGUE | COM | SEM | NOVAS | ERRO | INDEFINIDO
    """
    __tablename__ = "monitor_itens"
    __table_args__ = (UniqueConstraint("prestador_id", "item", "referencia", name="uq_monitor_item"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    prestador_id: Mapped[int] = mapped_column(ForeignKey("prestadores.id", ondelete="CASCADE"), index=True)
    item: Mapped[str] = mapped_column(String(20), index=True)
    referencia: Mapped[str] = mapped_column(String(10), default="")
    status: Mapped[str] = mapped_column(String(20), default="INDEFINIDO")
    resumo: Mapped[str] = mapped_column(String(300), default="")
    detalhe: Mapped[str] = mapped_column(Text, default="")            # JSON
    consulta_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    atualizado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    prestador: Mapped["Prestador"] = relationship()


class PagamentoRF(Base):
    """Pagamento (DARF/DAS/GPS) encontrado na Receita pelo Integra Contador (PAGTOWEB)."""
    __tablename__ = "pagamentos_rf"
    __table_args__ = (UniqueConstraint("prestador_id", "numero_documento", name="uq_pagamento_rf"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    prestador_id: Mapped[int] = mapped_column(ForeignKey("prestadores.id", ondelete="CASCADE"), index=True)
    numero_documento: Mapped[str] = mapped_column(String(40))
    tipo: Mapped[str] = mapped_column(String(40), default="")          # DAS, DARF…
    codigo_receita: Mapped[str] = mapped_column(String(20), default="")
    periodo_apuracao: Mapped[date | None] = mapped_column(Date, nullable=True)
    vencimento: Mapped[date | None] = mapped_column(Date, nullable=True)
    data_arrecadacao: Mapped[date | None] = mapped_column(Date, nullable=True)
    valor: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    em_atraso: Mapped[bool] = mapped_column(Boolean, default=False)
    detalhe: Mapped[str] = mapped_column(Text, default="")
    encontrado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    comprovante_consulta_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    prestador: Mapped["Prestador"] = relationship()


class GuiaRF(Base):
    """Guia (DAS, DAS-MEI, DARF DCTFWeb, parcela) gerada pelo Integra Contador. Só vencidas e parcelamentos."""
    __tablename__ = "guias_rf"
    id: Mapped[int] = mapped_column(primary_key=True)
    prestador_id: Mapped[int] = mapped_column(ForeignKey("prestadores.id", ondelete="CASCADE"), index=True)
    tipo: Mapped[str] = mapped_column(String(20), index=True)          # DAS | DAS_MEI | DARF_DCTFWEB | PARC_SN | PARC_MEI
    competencia: Mapped[str] = mapped_column(String(7), default="")    # 2026-09 (período ou parcela)
    parcela: Mapped[str] = mapped_column(String(20), default="")        # "10/12" (lida do DAS do parcelamento)
    numero_parcelamento: Mapped[str] = mapped_column(String(20), default="")
    numero_documento: Mapped[str] = mapped_column(String(40), default="", index=True)
    valor: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    vencimento: Mapped[date | None] = mapped_column(Date, nullable=True)   # "pagar este documento até"
    arquivo: Mapped[str] = mapped_column(String(300), default="")
    situacao: Mapped[str] = mapped_column(String(15), default="ABERTA", index=True)  # ABERTA | PAGA | SUBSTITUIDA
    gerada_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    gerada_por: Mapped[str] = mapped_column(String(40), default="")
    consulta_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pago_em: Mapped[date | None] = mapped_column(Date, nullable=True)
    pagamento_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    criterio_baixa: Mapped[str] = mapped_column(String(80), default="")
    entregue_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    erro: Mapped[str] = mapped_column(Text, default="")
    ec_doc_id: Mapped[str] = mapped_column(String(20), default="", index=True)      # documento no e-Contador (enviado pelo ERP)
    ec_enviada_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ec_baixada_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ec_excluida_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ec_erro: Mapped[str] = mapped_column(Text, default="")

    prestador: Mapped["Prestador"] = relationship()


class Recebimento(Base):
    """Honorário recebido (relatório do financeiro). Cada recebimento vira no máximo uma NFS-e do escritório."""
    __tablename__ = "recebimentos"
    __table_args__ = (UniqueConstraint("codigo_empresa", "titulo", name="uq_recebimento"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    codigo_empresa: Mapped[str] = mapped_column(String(20), index=True)
    nome: Mapped[str] = mapped_column(String(150), default="")
    titulo: Mapped[str] = mapped_column(String(30))
    categoria: Mapped[str] = mapped_column(String(60), default="")
    vencimento: Mapped[date | None] = mapped_column(Date, nullable=True)
    pagamento: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    valor: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    recebido: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    periodo_inicio: Mapped[date | None] = mapped_column(Date, nullable=True)
    periodo_fim: Mapped[date | None] = mapped_column(Date, nullable=True)
    arquivo_origem: Mapped[str] = mapped_column(String(200), default="")
    importado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    prestador_id: Mapped[int | None] = mapped_column(ForeignKey("prestadores.id", ondelete="SET NULL"), nullable=True)
    nota_id: Mapped[int | None] = mapped_column(ForeignKey("notas_fiscais.id", ondelete="SET NULL"), nullable=True)
    ec_doc_id: Mapped[str] = mapped_column(String(20), default="")                 # cobrança no e-Contador
    ec_baixado_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # baixa gravada e conferida pelo ERP
    ec_pago_em: Mapped[date | None] = mapped_column(Date, nullable=True)             # data que o e-Contador mostra
    ec_erro: Mapped[str] = mapped_column(Text, default="")

    prestador: Mapped["Prestador"] = relationship()
    nota: Mapped["NotaFiscal"] = relationship()


class ConsultaAPI(Base):
    """Chamadas às APIs avulsas do Serpro (Consulta CPF, Consulta CND): custo e resultado resumido."""
    __tablename__ = "consultas_api"
    id: Mapped[int] = mapped_column(primary_key=True)
    produto: Mapped[str] = mapped_column(String(10), index=True)      # CPF | CND
    documento: Mapped[str] = mapped_column(String(14), index=True)
    data_hora: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    usuario: Mapped[str] = mapped_column(String(40), default="")
    modo: Mapped[str] = mapped_column(String(15), default="")
    http: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    resumo: Mapped[str] = mapped_column(String(300), default="")
    dados: Mapped[str] = mapped_column(Text, default="")              # JSON sem o PDF
    custo: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)


class ConferenciaPGDAS(Base):
    """Faturamento do mês (NFS-e do ERP + NF-e/NFC-e do NF-Stock) x receita declarada no PGDAS-D."""
    __tablename__ = "conferencia_pgdas"

    id: Mapped[int] = mapped_column(primary_key=True)
    prestador_id: Mapped[int] = mapped_column(ForeignKey("prestadores.id", ondelete="CASCADE"), index=True)
    referencia: Mapped[str] = mapped_column(String(7), index=True)          # 2026-10
    nfse: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)
    qtd_nfse: Mapped[int] = mapped_column(Integer, default=0)
    nfe: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)
    qtd_nfe: Mapped[int] = mapped_column(Integer, default=0)
    nfce: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)
    qtd_nfce: Mapped[int] = mapped_column(Integer, default=0)
    faturamento_em: Mapped[datetime | None] = mapped_column(DateTime)
    erro_nfstock: Mapped[str] = mapped_column(String(300), default="")
    declarado: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)   # RPA
    rbt12: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    declarado_em: Mapped[datetime | None] = mapped_column(DateTime)
    consulta_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    arquivo_extrato: Mapped[str] = mapped_column(String(400), default="")   # extrato do PGDAS-D lido da pasta (Drive)
    obs: Mapped[str] = mapped_column(String(300), default="")

    prestador: Mapped["Prestador"] = relationship()

    @property
    def faturamento(self) -> Decimal:
        return (self.nfse or 0) + (self.nfe or 0) + (self.nfce or 0)
