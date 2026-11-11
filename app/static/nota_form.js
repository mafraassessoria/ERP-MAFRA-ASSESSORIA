/* Tela de emissão em etapas (Pessoas → Serviço → Valores → Emitir). */
(function () {
  var D = window.DADOS_NOTA || {};
  var form = document.getElementById("form-nota");
  var $ = function (s, r) { return (r || form).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || form).querySelectorAll(s)); };
  var etapaAtual = 1;

  function num(v) {
    v = (v || "").toString().trim();
    if (!v) return 0;
    if (v.indexOf(",") >= 0) v = v.replace(/\./g, "").replace(",", ".");
    else if (/^\d{1,3}(\.\d{3})+$/.test(v)) v = v.replace(/\./g, "");
    var n = parseFloat(v);
    return isNaN(n) ? 0 : n;
  }
  function br(n) { return n.toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function radio(nome) { var r = $('input[name="' + nome + '"]:checked'); return r ? r.value : ""; }
  function mostrar(el, sim) { if (el) el.style.display = sim ? "" : "none"; }
  function visivel(el) { return el && el.offsetParent !== null; }
  function textoOpcao(sel) { var o = sel.options[sel.selectedIndex]; return o && o.value ? o.text : ""; }

  // ---- Regras de exibição -------------------------------------------------------------
  function codigoServico() { return $("#ctribnac_outro").value || $("#ctribnac").value; }
  function ehObra() {
    var c = codigoServico();
    return !!c && (D.obraSubitens || []).indexOf(c.substring(0, 4)) >= 0;
  }

  function atualizar() {
    var tomSel = $("#tomador"), intSel = $("#intermediario");
    var temTomador = !!tomSel.value, temInterm = !!intSel.value;

    // Serviço
    var especial = radio("caso_especial") === "sim";
    mostrar($("#bloco-caso-especial"), especial);
    mostrar($("#bloco-imunidade"), especial && $("#tributacao_issqn").value === "2");
    $("#trib_vis").value = !especial ? "Operação tributável" :
      ({ "2": "Imunidade", "4": "Não incidência" }[$("#tributacao_issqn").value] || "—");
    var comp = $("#competencia").value;
    $("#competencia_vis").value = comp ? comp.split("-").reverse().join("/") : "";
    var mun = $("#municipio");
    $("#incidencia").value = mun.dataset.rotulo || "";

    // Obra
    var obra = ehObra();
    mostrar($("#bloco-obra"), obra);
    var obrasDoTomador = 0, selObra = $("#obra");
    Array.prototype.forEach.call(selObra.options, function (o) {
      if (!o.value) return;
      var ok = temTomador && o.dataset.tomador === tomSel.value;
      o.hidden = !ok; o.disabled = !ok;
      if (ok) obrasDoTomador++;
    });
    if (selObra.selectedOptions[0] && selObra.selectedOptions[0].disabled) selObra.value = "";
    if (obrasDoTomador === 0 && radio("obra_modo") === "existente" && temTomador) {
      $('input[name="obra_modo"][value="nova"]').checked = true;
    }
    if (obrasDoTomador === 1 && !selObra.value && radio("obra_modo") === "existente") {
      var unica = Array.prototype.filter.call(selObra.options, function (o) { return o.value && !o.disabled; })[0];
      if (unica) { selObra.value = unica.value; selObra.dispatchEvent(new Event("change")); }
    }
    var modo = radio("obra_modo") || "nova";
    mostrar($("#obra-existente"), modo === "existente");
    mostrar($("#obra-sem"), modo === "existente" && obrasDoTomador === 0);
    mostrar($("#obra-nova"), modo === "nova");
    var tipo = radio("obra_tipo") || "CNO";
    $$(".obra-CNO").forEach(function (e) { mostrar(e, tipo === "CNO"); });
    $$(".obra-CIB").forEach(function (e) { mostrar(e, tipo === "CIB"); });
    $$(".obra-END").forEach(function (e) { mostrar(e, tipo === "END"); });

    // Valores
    $("#vreceb").disabled = !temInterm;
    if (!temInterm) $("#vreceb").value = "";
    var retido = radio("iss_retido") === "sim";
    mostrar($("#bloco-retencao"), !especial);
    mostrar($("#bloco-retido"), retido && !especial);
    $("#ret-tomador").textContent = temTomador ? textoOpcao(tomSel) : "tomador não identificado";
    $("#ret-interm").textContent = temInterm ? textoOpcao(intSel) : "Não informado (escolha na etapa Pessoas)";
    var rInt = $('input[name="retido_por"][value="INTERMEDIARIO"]');
    rInt.disabled = !temInterm;
    if (!temInterm && rInt.checked) $('input[name="retido_por"][value="TOMADOR"]').checked = true;
    var rTom = $('input[name="retido_por"][value="TOMADOR"]');
    rTom.disabled = !temTomador;
    if (!temTomador && rTom.checked && temInterm) rInt.checked = true;

    mostrar($("#bloco-deducao"), obra && !especial);
    mostrar($("#sem-deducao"), !obra);
    mostrar($("#bloco-ded"), obra && radio("tem_deducao") === "sim");
    mostrar($("#bloco-federal"), !D.mei);

    var tt = radio("tot_trib_tipo") || "SN";
    $$(".tot-SN").forEach(function (e) { mostrar(e, tt === "SN"); });
    $$(".tot-campos").forEach(function (e) { mostrar(e, tt !== "SN"); });
    $$(".tot-campos label").forEach(function (l) {
      var base = l.textContent.replace(/\s*\((R\$|%)\)\s*$/, "").trim();
      l.firstChild.textContent = base + (tt === "PERCENTUAL" ? " (%) " : " (R$) ");
    });

    calcular();
    bloquearSequencia();
    atualizarNavegacao();
  }

  function calcular() {
    var valor = num($("#valor").value);
    var bc = valor - num($("#desc_incond").value);
    if (ehObra() && radio("tem_deducao") === "sim") {
      var dv = num($("#ded_valor").value);
      bc -= $("#ded_tipo").value === "PERCENTUAL" ? bc * dv / 100 : dv;
    }
    var retido = radio("iss_retido") === "sim" && radio("caso_especial") !== "sim";
    var aliq = num($("#aliquota").value);
    $("#calc_aliq").value = retido && aliq ? br(aliq) + " %" : "-";
    $("#calc_bc").value = valor ? "R$ " + br(Math.max(bc, 0)) : "-";
    $("#calc_iss").value = retido && aliq && valor ? "R$ " + br(Math.max(bc, 0) * aliq / 100) : "- (apurado no Simples)";
    var a = $("#aliquota");
    a.classList.toggle("invalido", retido && !!a.value && (aliq < 1.8 || aliq > 5));
  }

  // ---- Obrigatórios e liberação em sequência ----------------------------------------
  function campo(el) { return el && el._comboInput ? el._comboInput : el; }
  function obrigatorios(etapa) {
    var lista = [];
    if (etapa === 1) lista.push([$("#competencia"), "data de competência"]);
    if (etapa === 2) {
      lista.push([$("#municipio"), "município"]);
      lista.push([$("#ctribnac_outro").value ? $("#ctribnac_outro") : $("#ctribnac"), "código de tributação"]);
      if (radio("caso_especial") === "sim") {
        lista.push([$("#tributacao_issqn"), "situação (imunidade/não incidência)"]);
        if ($("#tributacao_issqn").value === "2") lista.push([$('select[name="tipo_imunidade"]'), "tipo de imunidade"]);
      }
      lista.push([$("#descricao"), "descrição do serviço"]);
      if (ehObra()) {
        if (!$("#tomador").value) lista.push([null, "tomador (obrigatório para obra)"]);
        if (radio("obra_modo") === "existente") lista.push([$("#obra"), "obra"]);
        else {
          var t = radio("obra_tipo");
          if (t === "CNO") lista.push([$('input[name="obra_cno"]'), "código da obra"]);
          else if (t === "CIB") lista.push([$('input[name="obra_cib"]'), "código CIB"]);
          else {
            lista.push([$('input[name="obra_cep"]'), "CEP da obra"]);
            lista.push([$('input[name="obra_logradouro"]'), "logradouro da obra"]);
            lista.push([$('input[name="obra_bairro"]'), "bairro da obra"]);
          }
        }
      }
    }
    if (etapa === 3) {
      lista.push([$("#valor"), "valor do serviço"]);
      if (radio("iss_retido") === "sim" && radio("caso_especial") !== "sim") {
        if (!$("#tomador").value && !$("#intermediario").value) lista.push([null, "tomador ou intermediário (para reter o ISS)"]);
        lista.push([$("#aliquota"), "alíquota do ISS (1,80% a 5,00%)", function (v) { var a = num(v); return a >= 1.8 && a <= 5; }]);
      }
      if (ehObra() && radio("tem_deducao") === "sim") lista.push([$("#ded_valor"), "valor da redução", function (v) { return num(v) > 0; }]);
      if ((radio("tot_trib_tipo") || "SN") !== "SN") {
        var algum = ["fed", "est", "mun"].some(function (k) { return num($('input[name="tot_trib_' + k + '"]').value) > 0; });
        if (!algum) lista.push([null, "valor aproximado dos tributos"]);
      }
    }
    return lista;
  }
  function preenchido(item) {
    var el = item[0];
    if (!el) return false;
    var v = (el.value || "").trim();
    if (!v) return false;
    if (el.id === "valor" && num(v) <= 0) return false;
    return item[2] ? item[2](v) : true;
  }
  function faltando(etapa) { return obrigatorios(etapa).filter(function (i) { return !preenchido(i); }); }

  function bloquearSequencia() {
    // Libera o próximo campo só depois que o obrigatório anterior estiver preenchido
    $$("[data-bloq]").forEach(function (e) { e.disabled = false; e.removeAttribute("data-bloq"); e.classList.remove("bloqueado"); });
    var painel = $('.etapa[data-etapa="' + etapaAtual + '"]');
    var primeiro = faltando(etapaAtual)[0];
    if (!primeiro || !primeiro[0]) return;
    var alvo = campo(primeiro[0]);
    var controles = $$("input, select, textarea", painel).filter(function (e) { return e.type !== "hidden" && !e.disabled && visivel(e); });
    var depois = false;
    controles.forEach(function (e) {
      if (e === alvo) { depois = true; return; }
      if (depois && e.type !== "radio" && !e.classList.contains("livre")) {
        e.disabled = true; e.setAttribute("data-bloq", "1"); e.classList.add("bloqueado");
      }
    });
  }

  // ---- Navegação entre etapas ---------------------------------------------------------
  function irPara(e) {
    etapaAtual = e;
    $$(".etapa").forEach(function (p) { mostrar(p, +p.dataset.etapa === e); });
    $$("#etapas button", document).forEach(function (b) {
      var n = +b.dataset.etapa;
      b.classList.toggle("atual", n === e);
      b.classList.toggle("feita", n < e);
    });
    if (e === 4) montarResumo();
    atualizar();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }
  function atualizarNavegacao() {
    var f = faltando(etapaAtual);
    $("#voltar").style.visibility = etapaAtual > 1 ? "visible" : "hidden";
    $("#avancar").style.display = etapaAtual < 4 ? "" : "none";
    $("#avancar").disabled = f.length > 0;
    $("#falta").textContent = f.length && etapaAtual < 4 ? "Falta: " + f.map(function (i) { return i[1]; }).join(", ") : "";
  }
  $("#voltar").addEventListener("click", function () { if (etapaAtual > 1) irPara(etapaAtual - 1); });
  $("#avancar").addEventListener("click", function () { if (!faltando(etapaAtual).length) irPara(etapaAtual + 1); });
  $$("#etapas button", document).forEach(function (b) {
    b.addEventListener("click", function () {
      var alvo = +b.dataset.etapa;
      for (var e = 1; e < alvo; e++) { if (faltando(e).length) { irPara(e); return; } }
      irPara(alvo);
    });
  });

  // ---- Resumo -----------------------------------------------------------------------
  function montarResumo() {
    var linhas = [];
    function add(r, v) { if (v) linhas.push("<dt>" + r + "</dt><dd>" + v.replace(/</g, "&lt;") + "</dd>"); }
    add("Tomador", textoOpcao($("#tomador")) || "Consumidor não identificado");
    add("Intermediário", textoOpcao($("#intermediario")));
    add("Competência", $("#competencia_vis").value);
    add("Local da prestação", $("#municipio").dataset.rotulo);
    add("Serviço", $("#ctribnac_outro").value ? $("#ctribnac_outro").dataset.rotulo : textoOpcao($("#ctribnac")));
    add("NBS", $("#nbs_outro").value ? $("#nbs_outro").dataset.rotulo : textoOpcao($("#nbs")));
    add("Situação do ISS", $("#trib_vis").value);
    add("Descrição", $("#descricao").value);
    if (ehObra()) add("Obra", radio("obra_modo") === "existente" ? textoOpcao($("#obra")) :
      (radio("obra_tipo") === "CNO" ? "CNO " + $('input[name="obra_cno"]').value :
        radio("obra_tipo") === "CIB" ? "CIB " + $('input[name="obra_cib"]').value : "Endereço " + $('input[name="obra_logradouro"]').value));
    add("Valor do serviço", "R$ " + br(num($("#valor").value)));
    var ret = radio("iss_retido") === "sim" && radio("caso_especial") !== "sim";
    add("ISS", ret ? "Retido pelo " + (radio("retido_por") === "INTERMEDIARIO" ? "intermediário" : "tomador") +
      " · alíquota " + $("#aliquota").value + "% · ISS " + $("#calc_iss").value : "Não retido (apurado no Simples)");
    var fed = [["IRRF", "ret_irrf"], ["CSLL", "ret_csll"], ["INSS", "ret_inss"]]
      .filter(function (x) { return num($('input[name="' + x[1] + '"]').value) > 0; })
      .map(function (x) { return x[0] + " R$ " + br(num($('input[name="' + x[1] + '"]').value)); });
    add("Retenções federais", fed.join(" · "));
    // Valor líquido = serviço − descontos − ISS retido − retenções federais
    var valor = num($("#valor").value);
    var descontos = num($("#desc_incond").value) + num($('input[name="desconto_condicionado"]').value);
    var issRet = 0;
    if (ret) {
      var bc = valor - num($("#desc_incond").value);
      if (ehObra() && radio("tem_deducao") === "sim") {
        var dv = num($("#ded_valor").value);
        bc -= $("#ded_tipo").value === "PERCENTUAL" ? bc * dv / 100 : dv;
      }
      issRet = Math.round(Math.max(bc, 0) * num($("#aliquota").value)) / 100;
    }
    var retFed = ["ret_irrf", "ret_csll", "ret_inss"].reduce(function (s, c) { return s + num($('input[name="' + c + '"]').value); }, 0);
    var liquido = valor - descontos - issRet - retFed;
    var partes = [];
    if (descontos) partes.push("descontos R$ " + br(descontos));
    if (issRet) partes.push("ISS retido R$ " + br(issRet));
    if (retFed) partes.push("retenções federais R$ " + br(retFed));
    linhas.push("<dt><b>Valor líquido da nota</b></dt><dd><b>R$ " + br(liquido) + "</b>" +
      (partes.length ? ' <span class="sutil">(R$ ' + br(valor) + " − " + partes.join(" − ") + ")</span>" : "") + "</dd>");
    add("Tributos aproximados", { SN: "Alíquota do Simples (" + br(D.pTotTribSN || 0) + "%)", VALOR: "Valores em R$", PERCENTUAL: "Percentuais" }[radio("tot_trib_tipo") || "SN"]);
    $("#resumo").innerHTML = linhas.join("");
  }

  // ---- Interações ---------------------------------------------------------------------
  function sugerirNbs() {
    var c = codigoServico(), alvo = $("#nbs-sugestoes");
    alvo.innerHTML = "";
    if (!c) return;
    fetch("/api/nbs?ctribnac=" + c, { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (lista) {
      if (!lista.length) return;
      var empresa = $$("#nbs option").map(function (o) { return o.value; });
      var sel = $("#nbs");
      // Se a NBS escolhida não combina com o serviço, avisa
      var ok = !sel.value || lista.some(function (x) { return x.codigo === sel.value; });
      alvo.innerHTML = (ok ? "" : "<b>A NBS escolhida não está na correlação oficial deste serviço.</b> ") +
        "NBS da tabela oficial para este serviço: ";
      if (lista.length > 8) {
        var mais = document.createElement("span");
        mais.textContent = "(+" + (lista.length - 8) + " em \"Usar outra NBS\")";
        setTimeout(function () { alvo.appendChild(mais); }, 0);
      }
      lista.slice(0, 8).forEach(function (x) {
        var a = document.createElement("a");
        a.href = "#"; a.textContent = x.rotulo.length > 70 ? x.rotulo.substring(0, 70) + "…" : x.rotulo; a.className = "sugestao";
        a.title = x.rotulo;
        a.addEventListener("click", function (ev) {
          ev.preventDefault();
          if (empresa.indexOf(x.codigo) >= 0) { sel.value = x.codigo; sel._comboAtualizar && sel._comboAtualizar(); $("#nbs_outro").value = ""; }
          else { var h = $("#nbs_outro"); h.value = x.codigo; h.dataset.rotulo = x.rotulo; h._comboInput.value = x.rotulo; h.closest("details").open = true; sel.value = ""; sel._comboAtualizar && sel._comboAtualizar(); }
          sugerirNbs(); atualizar();
        });
        alvo.appendChild(a);
      });
    }).catch(function () {});
  }

  $("#obra").addEventListener("change", function () {
    var o = (D.obras || []).filter(function (x) { return String(x.id) === $("#obra").value; })[0];
    if (o && o.mun) {
      fetch("/api/municipios?q=" + o.mun, { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (l) {
        var m = l.filter(function (x) { return x.codigo === o.mun; })[0];
        if (m) { var h = $("#municipio"); h.value = m.codigo; h.dataset.rotulo = m.nome; h._comboInput.value = m.nome; atualizar(); }
      });
    }
  });
  $("#sugerir-inss").addEventListener("click", function () {
    var v = num($("#valor").value);
    if (v) { $("#ret_inss").value = br(v * 0.11); atualizar(); }
  });
  ["#ctribnac", "#ctribnac_outro"].forEach(function (s) { $(s).addEventListener("change", function () { sugerirNbs(); }); });
  $("#nbs").addEventListener("change", sugerirNbs);
  form.addEventListener("input", atualizar);
  form.addEventListener("change", atualizar);

  $$("textarea[maxlength]").forEach(function (t) {
    var c = $('.contador[data-para="' + t.id + '"]');
    function conta() { if (c) c.textContent = "Total de caracteres: " + t.value.length + " / " + t.maxLength; }
    t.addEventListener("input", conta); conta();
  });

  form.addEventListener("submit", function (ev) {
    var botao = ev.submitter;
    for (var e = 1; e <= 3; e++) {
      if (faltando(e).length) { ev.preventDefault(); irPara(e); return; }
    }
    $$("[data-bloq]").forEach(function (x) { x.disabled = false; });
    $("#vreceb").disabled = false;
    if (botao) { setTimeout(function () { botao.disabled = true; botao.textContent = "Aguarde..."; }, 0); }
  });

  window.iniciarCombos && window.iniciarCombos(form);
  sugerirNbs();
  irPara(1);
})();
