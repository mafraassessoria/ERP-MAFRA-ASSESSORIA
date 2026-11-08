"""Emissão e cancelamento de NFS-e no Sistema Nacional (Sefin Nacional).

A montagem da DPS, a assinatura XMLDSIG, o transporte mTLS e o DANFSe usam a biblioteca
PyNFSeNacionalGT (MIT, validada em produção). Este módulo faz a ponte com o nosso banco:
numeração por prestador/ambiente, proteção contra duplicidade, arquivamento e auditoria.

Regras de segurança da emissão:
  • Uma emissão por prestador por vez (trava em memória).
  • O número da DPS fica gravado na nota ANTES do envio. Se a conexão cair, o reenvio
    usa o MESMO número e, antes de enviar, pergunta à Sefin se aquela DPS já virou nota
    (HEAD /dps/{id}). Assim a mesma nota nunca é emitida duas vezes.
  • Se o número já pertence a OUTRA nota na Sefin (contador atrasado), avança sozinho.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from lxml import etree
from sqlalchemy.orm import Session

from .. import models
from ..certificados import abrir_certificado
from ..config import DATA_DIR, XML_DIR

log = logging.getLogger("emissor.emissao")

VER_APLIC = "MR-EmissorNFSe-2.0"  # até 20 caracteres
MAX_NUMEROS_PULADOS = 20
OP_SIMP_NAC = {"MEI": 2, "SIMPLES": 3, "SIMPLES_EXCESSO": 3, "REGULAR": 1}
AMB_LIB = {"HOMOLOGACAO": "homologacao", "PRODUCAO": "producao"}

_travas: dict[int, threading.Lock] = {}
_travas_lock = threading.Lock()


class ErroEmissao(Exception):
    """Problema que impede a emissão (dados incompletos, certificado etc.)."""


@dataclass
class Resultado:
    ok: bool
    mensagem: str


def _trava(prestador_id: int) -> threading.Lock:
    with _travas_lock:
        return _travas.setdefault(prestador_id, threading.Lock())


def dono_certificado(db: Session, p: models.Prestador) -> models.Prestador:
    """Quem fornece o certificado A1: a própria empresa ou, com procuração eletrônica,
    o escritório (procurador). Pela regra da NFS-e Nacional (desde 01/11/2026), com procuração
    o certificado do procurador é usado na conexão (mTLS) e na assinatura da DPS/eventos;
    a DPS continua trazendo o CNPJ do prestador representado."""
    if not p.importar_por_procuracao:
        return p
    from ..configuracoes import ler_config
    pid = ler_config(db, "prestador_procurador_id")
    procurador = db.get(models.Prestador, int(pid)) if pid and pid.isdigit() else None
    if procurador is None:
        raise ErroEmissao("Procuração: defina em Configurações qual empresa é o escritório (procurador) "
                          "e envie o certificado dela.")
    return procurador


def certificado_para_evento(db: Session, p: models.Prestador) -> models.Prestador:
    """Certificado para eventos (cancelamento): precisa ter a mesma raiz de CNPJ do emitente."""
    cert = p.certificado_ativo
    if cert is not None and cert.validade_fim >= datetime.now():
        return p
    try:
        dono = dono_certificado(db, p)
    except ErroEmissao:
        dono = None
    if dono is not None and dono.cnpj[:8] == p.cnpj[:8]:
        return dono
    raise ErroEmissao(
        "Para cancelar, a Sefin exige o certificado da própria empresa (mesma raiz de CNPJ do emitente); "
        "a procuração do escritório não é aceita no cancelamento pela API (erros E0812/E0813). "
        "Envie o certificado A1 da empresa em Configurações da empresa — a emissão continua pela procuração — "
        "ou cancele no Emissor Nacional (gov.br/nfse), entrando com o certificado do escritório e usando "
        "'Representar' para esta empresa.")


def descricao_certificado(db: Session, p: models.Prestador) -> tuple[models.Prestador | None, str]:
    """(dono do certificado, problema ou ""), sem levantar exceção — para telas e conferência."""
    try:
        dono = dono_certificado(db, p)
    except ErroEmissao as e:
        return None, str(e)
    c = dono.certificado_ativo
    quem = "do escritório (procuração)" if dono.id != p.id else "da empresa"
    if c is None:
        return dono, f"Sem certificado A1 {quem}."
    if c.validade_fim < datetime.now():
        return dono, f"Certificado A1 {quem} vencido em {c.validade_fim:%d/%m/%Y}."
    return dono, ""


@contextmanager
def _certificado_temporario(prestador: models.Prestador):
    """Grava o .pfx descriptografado num arquivo temporário só durante a operação."""
    cert = prestador.certificado_ativo
    if cert is None:
        raise ErroEmissao("Prestador sem certificado A1 ativo.")
    if cert.validade_fim < datetime.now():
        raise ErroEmissao(f"Certificado A1 vencido em {cert.validade_fim:%d/%m/%Y}.")
    conteudo, senha = abrir_certificado(cert)
    pasta = DATA_DIR / "tmp"
    pasta.mkdir(parents=True, exist_ok=True)
    fd, caminho = tempfile.mkstemp(suffix=".pfx", dir=pasta)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(conteudo)
        yield caminho, senha
    finally:
        try:
            os.remove(caminho)
        except OSError:
            pass


# ---- Conversão do nosso cadastro para a configuração da biblioteca --------

def _config(prestador: models.Prestador, nota: models.NotaFiscal, pfx: str, senha: str):
    from pynfsenacional.config import ClienteConfig

    op = OP_SIMP_NAC.get(prestador.regime, 3)
    tributos: dict = {
        "tribISSQN": nota.tributacao_issqn or 1,
        "tpRetISSQN": nota.tipo_retencao_iss or 1,
        "piscofins_cst": prestador.cst_piscofins or "08",
    }
    if nota.tipo_ret_piscofins:
        tributos["tpRetPisCofins"] = nota.tipo_ret_piscofins
    if op == 3:
        tributos["pTotTribSN"] = f"{Decimal(prestador.p_tot_trib_sn or 0):.2f}"
    else:
        tributos["indTotTrib"] = 0
    return ClienteConfig.from_dict({
        "id": str(prestador.id),
        "nome_cliente": prestador.razao_social,
        "ambiente": AMB_LIB[prestador.ambiente],
        "certificado": {"path": pfx, "senha": senha},
        "prestador": {
            "cnpj": prestador.cnpj,
            "codigo_municipio": prestador.codigo_municipio_ibge,
            "uf": prestador.uf or None,
            "inscricao_municipal": prestador.inscricao_municipal or None,
            "regime": {
                "opSimpNac": op,
                "regApTribSN": prestador.reg_ap_trib_sn or 1,
                "regEspTrib": prestador.reg_esp_trib or 0,
            },
        },
        "servico": {
            "codigo_municipio_prestacao": nota.municipio_prestacao or prestador.codigo_municipio_ibge,
            "cTribNac": nota.codigo_tributacao_nacional,
            "cTribMun": nota.codigo_tributacao_municipal or "",
            "descricao_padrao": nota.descricao_servico,
            "tributos": tributos,
            "info_complementares": nota.info_complementares or "",
            "cNBS": nota.codigo_nbs or "",
        },
    })


def _tomador(nota: models.NotaFiscal) -> dict | None:
    t = nota.tomador
    if t is None:
        return None
    dados = {"cpf_cnpj": t.documento, "nome": t.nome, "im": t.inscricao_municipal,
             "email": t.email, "telefone": t.telefone}
    if t.codigo_municipio_ibge and t.cep and t.logradouro and t.bairro:
        dados["endereco"] = {"codigo_municipio": t.codigo_municipio_ibge, "cep": t.cep,
                             "logradouro": t.logradouro, "numero": t.numero,
                             "complemento": t.complemento, "bairro": t.bairro}
    return dados


def dados_extras(nota: models.NotaFiscal):
    from .dps_extra import DadosExtras
    return DadosExtras(
        obra=nota.obra, ded_tipo=nota.ded_tipo, ded_valor=nota.ded_valor, aliquota_iss=nota.aliquota_iss,
        tp_ret_iss=nota.tipo_retencao_iss, ret_inss=nota.ret_inss, ret_irrf=nota.ret_irrf, ret_csll=nota.ret_csll,
        intermediario=nota.intermediario, valor_recebido_intermediario=nota.valor_recebido_intermediario,
        desconto_incondicionado=nota.desconto_incondicionado, desconto_condicionado=nota.desconto_condicionado,
        tipo_imunidade=nota.tipo_imunidade if (nota.tributacao_issqn or 1) == 2 else None,
        tot_trib_tipo=nota.tot_trib_tipo or "SN", tot_trib_fed=nota.tot_trib_fed,
        tot_trib_est=nota.tot_trib_est, tot_trib_mun=nota.tot_trib_mun)


def conferir(db: Session, nota: models.NotaFiscal) -> list[str]:
    """Problemas que impedem a emissão — verificados antes de tocar a Sefin."""
    try:
        from pynfsenacional.config import validar_retencao, validar_tomador
    except ImportError:
        return ["Componente da NFS-e Nacional não instalado. Execute o ATUALIZAR_SISTEMA.bat no servidor."]

    p = nota.prestador
    problemas: list[str] = []
    if not p.emite_nfse:
        problemas.append("Este prestador está no modo 'somente importação'.")
    if p.regime == "REGULAR":
        problemas.append("Regime Regular ainda não habilitado neste emissor (exige o grupo IBS/CBS). "
                         "Será incluído em uma próxima etapa.")
    if not p.codigo_municipio_ibge:
        problemas.append("Informe o código IBGE do município do prestador.")
    if p.regime in ("SIMPLES", "SIMPLES_EXCESSO") and not p.p_tot_trib_sn and (nota.tot_trib_tipo or "SN") == "SN":
        problemas.append("Informe no cadastro do prestador o % aproximado de tributos do Simples "
                         "(campo obrigatório para ME/EPP — E0712).")
    _dono, problema_cert = descricao_certificado(db, p)
    if problema_cert:
        problemas.append(problema_cert)
    if Decimal(nota.valor_servicos or 0) <= 0:
        problemas.append("O valor do serviço deve ser maior que zero.")
    if not nota.descricao_servico.strip():
        problemas.append("Informe a descrição do serviço.")
    if nota.competencia and nota.competencia > date.today():
        problemas.append("A competência não pode ser futura.")
    if problemas:
        return problemas
    try:
        cfg = _config(p, nota, "-", "-")
    except Exception as e:  # noqa: BLE001
        return [f"Configuração inválida: {e}"]
    tom = _tomador(nota)
    lib = cfg.validar() + validar_tomador(tom) + validar_retencao(cfg, tom)
    # O grupo <interm> é montado por este sistema (dps_extra)
    if nota.intermediario is not None:
        lib = [x for x in lib if not x.startswith("tpRetISSQN=3")]
    # A biblioteca base não emite pAliq; este sistema emite (dps_extra). Só vale o aviso se faltar a alíquota.
    if Decimal(nota.aliquota_iss or 0) > 0:
        lib = [x for x in lib if not x.startswith("E0621")]
    else:
        lib = [("ISS retido: informe a alíquota do ISS (%) na nota (E0621)." if x.startswith("E0621") else x) for x in lib]
    return lib + _conferir_extras(nota, db)


ALIQ_ISS_MIN, ALIQ_ISS_MAX = Decimal("1.80"), Decimal("5.00")


def _conferir_extras(nota: models.NotaFiscal, db: Session | None = None) -> list[str]:
    """Obra, dedução, retenções, intermediário, descontos e tributos aproximados."""
    from . import tabelas
    from .dps_extra import problemas_obra
    p = nota.prestador
    problemas: list[str] = []
    if len(nota.codigo_tributacao_nacional or "") != 6:
        problemas.append("Escolha o código de tributação nacional do serviço.")
    if db is not None and tabelas.exige_obra(db, nota.codigo_tributacao_nacional) and nota.obra is None:
        problemas.append("Serviço de obra: informe a obra (CNO, CIB ou endereço) na nota.")
    if nota.municipio_prestacao and nota.municipio_prestacao not in tabelas.municipios():
        problemas.append("Município do local da prestação não encontrado na tabela do IBGE.")
    if (nota.tributacao_issqn or 1) == 2 and nota.tipo_imunidade is None:
        problemas.append("Imunidade: informe o tipo de imunidade.")
    if (nota.tributacao_issqn or 1) == 3:
        problemas.append("Exportação de serviço ainda não é emitida por este sistema.")
    ret = nota.tipo_retencao_iss or 1
    aliq = Decimal(nota.aliquota_iss or 0)
    if ret in (2, 3) and not (ALIQ_ISS_MIN <= aliq <= ALIQ_ISS_MAX):
        problemas.append("ISS retido: a alíquota deve ficar entre 1,80% e 5,00%.")
    if ret == 3 and nota.intermediario is None:
        problemas.append("ISS retido pelo intermediário: escolha o intermediário.")
    if ret == 2 and nota.tomador is None:
        problemas.append("ISS retido pelo tomador: a nota precisa de tomador identificado.")
    if Decimal(nota.valor_recebido_intermediario or 0) > 0 and nota.intermediario is None:
        problemas.append("Valor recebido pelo intermediário informado sem intermediário.")
    if (nota.tot_trib_tipo or "SN") in ("VALOR", "PERCENTUAL") and not any(
            Decimal(x or 0) > 0 for x in (nota.tot_trib_fed, nota.tot_trib_est, nota.tot_trib_mun)):
        problemas.append("Valor aproximado dos tributos: informe ao menos um valor (federal, estadual ou municipal).")
    problemas += problemas_obra(nota.obra)
    valor = Decimal(nota.valor_servicos or 0)
    if nota.ded_tipo == "PERCENTUAL" and not (0 < Decimal(nota.ded_valor or 0) < 100):
        problemas.append("Dedução em percentual deve ficar entre 0 e 100%.")
    if nota.ded_tipo == "VALOR" and not (0 < Decimal(nota.ded_valor or 0) < valor):
        problemas.append("Dedução em valor deve ser maior que zero e menor que o valor do serviço.")
    rets = [Decimal(x or 0) for x in (nota.ret_inss, nota.ret_irrf, nota.ret_csll)]
    if any(r < 0 for r in rets) or sum(rets) >= valor > 0:
        problemas.append("Retenções federais inválidas (negativas ou maiores que o valor do serviço).")
    if p.regime == "MEI" and any(r > 0 for r in rets):
        problemas.append("MEI não informa retenções federais na NFS-e (E0676).")
    descontos = Decimal(nota.desconto_incondicionado or 0) + Decimal(nota.desconto_condicionado or 0)
    if descontos < 0 or (valor > 0 and descontos >= valor):
        problemas.append("Descontos inválidos (negativos ou maiores que o valor do serviço).")
    return problemas


# ---- Arquivamento ------------------------------------------------------------

def _pasta_documentos(nota: models.NotaFiscal) -> Path:
    ref = nota.competencia or date.today()
    pasta = XML_DIR / nota.prestador.cnpj / f"{ref:%Y}" / f"{ref:%m}"
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta


def _texto(raiz, tag: str) -> str | None:
    el = raiz.find(f".//{{*}}{tag}")
    return el.text.strip() if el is not None and el.text else None


def _arquivar_nfse(nota: models.NotaFiscal, xml: str, chave: str | None) -> None:
    from pynfsenacional import chave as libchave

    chave = chave or libchave.chave_do_xml(xml)
    nota.chave_acesso = chave
    prefixo = ("HOMOLOG_" if nota.ambiente == "HOMOLOGACAO" else "")
    pasta = _pasta_documentos(nota)
    nome = f"{prefixo}NFSe_{chave or nota.id_dps}"
    caminho_xml = pasta / f"{nome}.xml"
    caminho_xml.write_text(xml, encoding="utf-8")
    nota.caminho_xml = str(caminho_xml.relative_to(XML_DIR))
    from sqlalchemy.orm import object_session
    from ..configuracoes import copiar_para_pasta_extra
    sessao = object_session(nota)
    if sessao is not None and nota.ambiente == "PRODUCAO":
        copiar_para_pasta_extra(sessao, nota.prestador.cnpj, caminho_xml)
    try:
        raiz = etree.fromstring(xml.encode("utf-8"))
        nota.numero_nfse = _texto(raiz, "nNFSe") or libchave.numero_da_chave(chave)
        v = _texto(raiz, "vISSQN")
        nota.valor_iss = Decimal(v) if v else Decimal("0")
        dh = _texto(raiz, "dhProc")
        if dh:
            nota.data_emissao = datetime.fromisoformat(dh).replace(tzinfo=None)
    except Exception as e:  # noqa: BLE001
        log.warning("não foi possível ler dados do XML da NFS-e: %s", e)
    try:
        caminho_pdf = pasta / f"{nome}.pdf"
        render_danfse(xml, caminho_pdf, cancelada=nota.situacao == "CANCELADA")
        nota.caminho_pdf = str(caminho_pdf.relative_to(XML_DIR))
    except Exception as e:  # noqa: BLE001
        log.warning("DANFSe não gerado: %s", e)
        nota.mensagem_retorno = (nota.mensagem_retorno + f"\nDANFSe não gerado: {e}").strip()


def _refazer_pdf(nota: models.NotaFiscal) -> None:
    """Depois do cancelamento, o PDF passa a sair com a marca CANCELADA."""
    if nota.caminho_xml and (XML_DIR / nota.caminho_xml).exists():
        try:
            gerar_danfse(nota)
        except Exception as e:  # noqa: BLE001
            log.warning("DANFSe da nota cancelada não regerado: %s", e)


def render_danfse(xml: str, destino: Path, cancelada: bool = False) -> None:
    """DANFSe v2.0 (NT 008/2026) com o canhoto no rodapé, como o do Emissor Nacional:
    Data cientificação / Identificação e assinatura / Nº NFS-e e chave.
    Nota cancelada sai com a marca d'água CANCELADA."""
    try:
        from brazilfiscalreport.danfse import Danfse, DanfseConfig
    except ImportError as e:
        raise ImportError("geração do DANFSe requer o componente BrazilFiscalReport (rode ATUALIZAR_SISTEMA.bat)") from e
    destino.parent.mkdir(parents=True, exist_ok=True)
    try:
        cfg = DanfseConfig(display_canhoto=True, watermark_cancelled=cancelada)
    except TypeError as e:  # versão antiga do componente
        raise ImportError("componente BrazilFiscalReport desatualizado (rode ATUALIZAR_SISTEMA.bat)") from e
    Danfse(xml=xml, config=cfg).output(str(destino))


def gerar_danfse(nota: models.NotaFiscal) -> None:
    """Gera (de novo) o PDF a partir do XML arquivado."""
    xml = (XML_DIR / nota.caminho_xml).read_text(encoding="utf-8")
    caminho_pdf = (XML_DIR / nota.caminho_xml).with_suffix(".pdf")
    render_danfse(xml, caminho_pdf, cancelada=nota.situacao == "CANCELADA")
    nota.caminho_pdf = str(caminho_pdf.relative_to(XML_DIR))


# ---- Numeração ---------------------------------------------------------------

def _proximo_numero(p: models.Prestador) -> int:
    return p.proximo_numero_dps_homologacao if p.ambiente == "HOMOLOGACAO" else p.proximo_numero_dps


def _avancar_contador(p: models.Prestador, usado: int) -> None:
    if p.ambiente == "HOMOLOGACAO":
        p.proximo_numero_dps_homologacao = max(p.proximo_numero_dps_homologacao or 1, usado + 1)
    else:
        p.proximo_numero_dps = max(p.proximo_numero_dps or 1, usado + 1)


def _numero_em_uso(db: Session, p: models.Prestador, numero: int, nota_id: int) -> bool:
    return db.query(models.NotaFiscal).filter(
        models.NotaFiscal.prestador_id == p.id, models.NotaFiscal.ambiente == p.ambiente,
        models.NotaFiscal.serie_dps == p.serie_dps, models.NotaFiscal.numero_dps == numero,
        models.NotaFiscal.id != nota_id,
    ).first() is not None


def _reservar_numero(db: Session, nota: models.NotaFiscal) -> None:
    """Dá à nota um número de DPS (se ainda não tem) e grava ANTES do envio."""
    p = nota.prestador
    if nota.numero_dps and nota.ambiente == p.ambiente and nota.serie_dps == p.serie_dps:
        return
    numero = _proximo_numero(p)
    while _numero_em_uso(db, p, numero, nota.id):
        numero += 1
    nota.ambiente = p.ambiente
    nota.serie_dps = p.serie_dps
    nota.numero_dps = numero
    _avancar_contador(p, numero)
    db.commit()


# ---- Emissão -----------------------------------------------------------------

def emitir(db: Session, nota: models.NotaFiscal, usuario: str) -> Resultado:
    try:
        from pynfsenacional import assinatura, dps, sefin
        from pynfsenacional.emissor import mesma_nota
    except ImportError:
        return Resultado(False, "Componente da NFS-e Nacional não instalado. Execute o ATUALIZAR_SISTEMA.bat no servidor.")

    if nota.situacao in ("AUTORIZADA", "CANCELADA"):
        return Resultado(False, "Esta nota já foi autorizada.")
    problemas = conferir(db, nota)
    if problemas:
        nota.mensagem_retorno = "\n".join(problemas)
        db.commit()
        return Resultado(False, "Corrija antes de emitir:\n- " + "\n- ".join(problemas))

    trava = _trava(nota.prestador_id)
    if not trava.acquire(timeout=1):
        return Resultado(False, "Já existe uma emissão em andamento para este prestador. Aguarde.")
    try:
        p = nota.prestador
        _reservar_numero(db, nota)
        nota.situacao = "PROCESSANDO"
        nota.emitida_por = usuario
        db.commit()

        with _certificado_temporario(dono_certificado(db, p)) as (pfx, senha):
            cfg = _config(p, nota, pfx, senha)
            amb = AMB_LIB[p.ambiente]
            cliente = sefin.SefinClient(ambiente=amb, cert_pfx_path=pfx, senha=senha,
                                        codigo_municipio=p.codigo_municipio_ibge)
            tomador = _tomador(nota)
            serie = str(int(p.serie_dps))

            # 1) Esta DPS já virou nota? (envio anterior sem resposta, ou contador atrasado)
            for _ in range(MAX_NUMEROS_PULADOS + 1):
                nota.id_dps = dps.id_dps(cfg, str(nota.numero_dps), serie)
                db.commit()
                if not cliente.verificar_dps(nota.id_dps):
                    break
                chave = cliente.consultar_dps(nota.id_dps)
                xml = cliente.consultar_nfse(chave) if chave else None
                if mesma_nota(xml, nota.valor_servicos, tomador) is False:
                    log.warning("nDPS %s já pertence a outra nota; avançando", nota.numero_dps)
                    _avancar_contador(p, nota.numero_dps)
                    nota.numero_dps = None
                    _reservar_numero(db, nota)
                    continue
                # É esta nota: recupera em vez de reemitir
                if xml:
                    _arquivar_nfse(nota, xml, chave)
                else:
                    nota.chave_acesso = chave
                nota.situacao = "AUTORIZADA"
                nota.mensagem_retorno = "Nota recuperada: esta DPS já havia sido autorizada pela Sefin."
                db.commit()
                return Resultado(True, f"Nota já estava autorizada (recuperada). Nº {nota.numero_nfse or '-'}")
            else:
                raise ErroEmissao("Contador de DPS muito atrasado em relação às notas já emitidas. "
                                  "Ajuste o próximo número no cadastro do prestador.")

            # 2) Monta, assina e envia
            xml_dps = dps.montar_dps(
                cfg, numero=str(nota.numero_dps), valor=nota.valor_servicos,
                descricao=nota.descricao_servico, tomador=tomador, ambiente=amb, serie=serie,
                d_comp=(nota.competencia or date.today()).isoformat(), ver_aplic=VER_APLIC,
            )
            from .dps_extra import DadosExtras, complementar
            xml_dps = complementar(xml_dps, dados_extras(nota))
            xml_assinado = assinatura.assinar_dps(xml_dps, cert_pfx_path=pfx, senha=senha)
            (_pasta_documentos(nota) / f"DPS_{nota.id_dps}.xml").write_text(xml_assinado, encoding="utf-8")
            resp = cliente.emitir(xml_assinado)

        if resp.ok:
            _arquivar_nfse(nota, resp.nfse_xml or "", resp.chave_acesso)
            if not nota.numero_nfse and resp.numero_nfse:
                nota.numero_nfse = resp.numero_nfse
            nota.situacao = "AUTORIZADA"
            alertas = "; ".join(resp.alertas or [])
            nota.mensagem_retorno = f"Autorizada. {('Alertas: ' + alertas) if alertas else ''}".strip()
            db.commit()
            return Resultado(True, f"NFS-e autorizada! Número {nota.numero_nfse or '-'}.")

        msg = f"[{resp.erro_codigo}] {resp.erro_msg}" if resp.erro_codigo else (resp.erro_msg or "Erro desconhecido")
        nota.situacao = "REJEITADA"
        nota.mensagem_retorno = msg
        db.commit()
        return Resultado(False, f"Sefin recusou a nota: {msg}")

    except ErroEmissao as e:
        nota.situacao = "REJEITADA" if nota.situacao == "PROCESSANDO" else nota.situacao
        nota.mensagem_retorno = str(e)
        db.commit()
        return Resultado(False, str(e))
    except Exception as e:  # noqa: BLE001 — rede, timeout, Sefin fora do ar
        log.exception("falha na emissão da nota %s", nota.id)
        nota.situacao = "PROCESSANDO"
        nota.mensagem_retorno = (f"Sem resposta conclusiva da Sefin ({type(e).__name__}: {e}). "
                                 "Clique em 'Transmitir' de novo: o sistema verifica antes se a nota "
                                 "já foi autorizada, sem risco de duplicar.")
        db.commit()
        return Resultado(False, nota.mensagem_retorno)
    finally:
        trava.release()


# ---- Cancelamento ------------------------------------------------------------

def cancelar(db: Session, nota: models.NotaFiscal, usuario: str, motivo: int, justificativa: str) -> Resultado:
    from pynfsenacional import assinatura, eventos, sefin

    if nota.situacao != "AUTORIZADA" or not nota.chave_acesso:
        return Resultado(False, "Só é possível cancelar nota autorizada.")
    if nota.origem != "EMITIDA":
        return Resultado(False, "Nota importada: o cancelamento deve ser feito por quem a emitiu.")
    justificativa = " ".join(justificativa.split())
    if not 15 <= len(justificativa) <= 200:
        return Resultado(False, "A justificativa deve ter entre 15 e 200 caracteres.")
    if motivo not in models.MOTIVOS_CANCELAMENTO:
        return Resultado(False, "Motivo inválido.")
    p = nota.prestador
    amb = AMB_LIB[nota.ambiente]
    try:
        dono = certificado_para_evento(db, p)
        # Regras da Sefin: o autor do cancelamento é o emitente da nota (E0813) e o certificado
        # que assina tem de ter a mesma raiz de CNPJ do autor (E0812). Por isso a procuração do
        # escritório não serve para cancelar pela API: é preciso o certificado da própria empresa.
        with _certificado_temporario(dono) as (pfx, senha):
            xml = eventos.montar_cancelamento(chave=nota.chave_acesso, autor=p.cnpj, motivo=motivo,
                                              justificativa=justificativa, ambiente=amb, modo="direto")
            assinado = assinatura.assinar_xml(xml, "infPedReg", cert_pfx_path=pfx, senha=senha)
            cliente = sefin.SefinClient(ambiente=amb, cert_pfx_path=pfx, senha=senha,
                                        codigo_municipio=p.codigo_municipio_ibge)
            r = cliente.registrar_evento(nota.chave_acesso, assinado)
    except ErroEmissao as e:
        return Resultado(False, str(e))
    except ValueError as e:  # ConfigError da biblioteca: pedido inválido, nada foi enviado
        return Resultado(False, f"Pedido de cancelamento inválido (não enviado): {e}")
    except Exception as e:  # noqa: BLE001
        log.exception("falha no cancelamento da nota %s", nota.id)
        return Resultado(False, f"Sem resposta da Sefin ({type(e).__name__}). Consulte a nota antes de tentar de novo.")

    if not r.ok:
        msg = f"[{r.erro_codigo}] {r.erro_msg}" if r.erro_codigo else (r.erro_msg or "Erro desconhecido")
        return Resultado(False, f"Sefin recusou o cancelamento: {msg}")
    if r.evento_xml:
        caminho = _pasta_documentos(nota) / f"{'HOMOLOG_' if nota.ambiente == 'HOMOLOGACAO' else ''}CANC_{nota.chave_acesso}.xml"
        caminho.write_text(r.evento_xml, encoding="utf-8")
        nota.caminho_evento_xml = str(caminho.relative_to(XML_DIR))
    nota.situacao = "CANCELADA"
    nota.cancelada_em = datetime.now()
    nota.cancelada_por = usuario
    nota.motivo_cancelamento = f"{models.MOTIVOS_CANCELAMENTO[motivo]}: {justificativa}"
    _refazer_pdf(nota)
    db.commit()
    return Resultado(True, "Nota cancelada na Sefin.")
