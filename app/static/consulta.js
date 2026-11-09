// Preenche o formulário a partir do CNPJ (dados abertos da Receita), do CPF (Consulta CPF – Serpro) e do CEP.
(function () {
  const CAMPOS = ["nome_fantasia", "cep", "logradouro", "numero", "complemento", "bairro",
                  "municipio", "uf", "codigo_municipio_ibge", "telefone", "email"];

  function aviso(form, texto, erro) {
    const el = form.querySelector(".aviso-consulta");
    if (!el) return;
    el.textContent = texto;
    el.className = "aviso-consulta msg " + (erro ? "erro" : "");
    el.style.display = texto ? "block" : "none";
  }

  function preencher(form, nome, valor, sobrescrever) {
    const el = form.elements[nome];
    if (!el || !valor) return;
    if (sobrescrever || !el.value.trim()) el.value = valor;
  }

  async function obterJSON(url) {
    const r = await fetch(url, { headers: { "Accept": "application/json" }, credentials: "same-origin" });
    const tipo = r.headers.get("content-type") || "";
    if (!tipo.includes("json")) throw new Error("Sessão expirada. Entre de novo no sistema.");
    const d = await r.json();
    if (!r.ok) throw new Error(d.erro || "Falha na consulta.");
    return d;
  }

  window.buscarCNPJ = async function (btn) {
    const form = btn.closest("form");
    const campoDoc = form.querySelector("[data-doc]");
    const campoNome = campoDoc.dataset.nome || "razao_social";
    const doc = campoDoc.value.replace(/[^0-9A-Za-z]/g, "").toUpperCase();
    if (doc.length === 11) { return buscarCPF(btn, form, campoNome, doc); }
    if (doc.length !== 14) { aviso(form, "Digite o CPF (11 dígitos) ou o CNPJ (14 caracteres) completo.", true); return; }
    const textoOriginal = btn.textContent;
    btn.disabled = true; btn.textContent = "Consultando...";
    try {
      const d = await obterJSON("/api/cnpj/" + doc);
      const novo = !(form.elements["id"] && form.elements["id"].value);
      preencher(form, campoNome, d.razao_social, true);
      CAMPOS.forEach(c => preencher(form, c, d[c], true));
      if (form.elements["regime"] && novo) {
        if (d.mei === true) form.elements["regime"].value = "MEI";
        else if (d.simples === true) form.elements["regime"].value = "SIMPLES";
      }
      const partes = ["Dados preenchidos (" + d.fonte + ", base pública da Receita)."];
      if (d.situacao) partes.push("Situação: " + d.situacao + ".");
      if (d.mei === true) partes.push("Optante MEI.");
      else if (d.simples === true) partes.push("Optante do Simples Nacional.");
      else if (d.simples === false) partes.push("Não optante do Simples.");
      if (d.cnae) partes.push("CNAE: " + d.cnae + ".");
      partes.push("Confira antes de salvar.");
      aviso(form, partes.join(" "), d.situacao && d.situacao !== "ATIVA");
    } catch (e) {
      aviso(form, e.message, true);
    } finally {
      btn.disabled = false; btn.textContent = textoOriginal;
    }
  };

  // CPF: Consulta CPF da Receita (Serpro). Exige a data de nascimento; se o formulário não tiver o campo,
  // ele é criado logo abaixo do CPF.
  function campoNascimento(form) {
    let el = form.elements["data_nascimento"];
    if (el) return el;
    const doc = form.querySelector("[data-doc]");
    const label = document.createElement("label");
    label.setAttribute("data-nascimento", "");
    label.innerHTML = 'Data de nascimento <small class="sutil">(exigida pela consulta de CPF na Receita)</small> <input type="date" name="data_nascimento">';
    const ancora = doc.closest("label") || doc;
    ancora.insertAdjacentElement("afterend", label);
    return form.elements["data_nascimento"];
  }

  async function buscarCPF(btn, form, campoNome, doc) {
    const nasc = campoNascimento(form);
    if (!nasc.value) {
      aviso(form, "Para consultar o CPF na Receita, informe a data de nascimento — a consulta roda sozinha ao preencher.", false);
      nasc.dataset.cpfPendente = "1";
      nasc.focus();
      return;
    }
    const textoOriginal = btn.textContent;
    btn.disabled = true; btn.textContent = "Consultando...";
    try {
      const d = await obterJSON("/api/cpf/" + doc + "?nasc=" + encodeURIComponent(nasc.value));
      preencher(form, campoNome, d.nome, true);
      CAMPOS.concat(["inscricao_municipal"]).forEach(c => preencher(form, c, d[c], false));
      const partes = ["Nome conforme a " + d.fonte + "."];
      if (d.situacao) partes.push("Situação do CPF: " + d.situacao + ".");
      if (d.nome_social) partes.push("Nome social: " + d.nome_social + ".");
      if (d.complemento_fonte) partes.push("Endereço/contatos completados com o " + d.complemento_fonte + ".");
      else partes.push("A Receita não informa endereço: preencha pelo CEP.");
      partes.push("Confira antes de salvar.");
      aviso(form, partes.join(" "), d.regular === false);
    } catch (e) {
      aviso(form, e.message, true);
    } finally {
      btn.disabled = false; btn.textContent = textoOriginal;
    }
  }

  // Ao preencher a data de nascimento depois de pedir a consulta, consulta sozinho
  document.addEventListener("change", function (ev) {
    const el = ev.target;
    if (el.name !== "data_nascimento" || !el.value || el.value.length !== 10) return;
    const form = el.closest("form");
    const docEl = form && form.querySelector("[data-doc]");
    if (!docEl) return;
    const doc = docEl.value.replace(/\D/g, "");
    const nome = form.elements[docEl.dataset.nome || "razao_social"];
    if (doc.length === 11 && (el.dataset.cpfPendente === "1" || (nome && !nome.value.trim()))) {
      el.dataset.cpfPendente = "";
      const btn = docEl.parentElement.querySelector("button");
      if (btn) buscarCPF(btn, form, docEl.dataset.nome || "razao_social", doc);
    }
  });

  // Cadastro novo: ao sair do campo CPF/CNPJ com o nome vazio, já consulta
  document.addEventListener("change", function (ev) {
    const el = ev.target;
    if (!el.matches || !el.matches("[data-doc]")) return;
    const form = el.closest("form");
    const nome = form.elements[el.dataset.nome || "razao_social"];
    const btn = el.parentElement.querySelector("button");
    const doc = el.value.replace(/[^0-9A-Za-z]/g, "");
    if (btn && nome && !nome.value.trim() && (doc.length === 11 || doc.length === 14)) window.buscarCNPJ(btn);
  });

  // CEP: ao sair do campo, completa endereço e código IBGE
  document.addEventListener("change", async function (ev) {
    const el = ev.target;
    if (el.name !== "cep") return;
    const cep = el.value.replace(/\D/g, "");
    if (cep.length !== 8) return;
    const form = el.closest("form");
    try {
      const d = await obterJSON("/api/cep/" + cep);
      preencher(form, "logradouro", d.logradouro, false);
      preencher(form, "bairro", d.bairro, false);
      preencher(form, "municipio", d.municipio, true);
      preencher(form, "uf", d.uf, true);
      preencher(form, "codigo_municipio_ibge", d.codigo_municipio_ibge, true);
    } catch (e) {
      aviso(form, "CEP: " + e.message, true);
    }
  });
})();
