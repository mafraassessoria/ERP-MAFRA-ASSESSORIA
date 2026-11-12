/* Caixas de seleção com pesquisa ao digitar.
 *
 *  <select class="combo">            → as opções do próprio select, filtradas ao digitar
 *  <input type="hidden" class="combo-api" data-api="/api/municipios" data-rotulo="Betim/MG" value="3106705">
 *                                     → busca no servidor (?q=), mínimo 2 letras
 *  Opcional: data-desc="nome_do_campo" grava a descrição (texto depois de " - ") em outro campo do formulário.
 *  Ao escolher, dispara "change" no elemento original (select ou hidden).
 *
 *  <input data-filtra-tabela="id_da_tabela"> → esconde as linhas da tabela que não contêm o texto.
 */
(function () {
  function semAcento(t) { return (t || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase(); }

  function criarCaixa(orig, rotuloInicial, placeholder) {
    var box = document.createElement("div");
    box.className = "combo-caixa";
    var inp = document.createElement("input");
    inp.type = "text"; inp.autocomplete = "off";
    inp.placeholder = placeholder || "Digite para pesquisar...";
    inp.value = rotuloInicial || "";
    if (orig.required) inp.dataset.obrigatorio = "1";
    var lista = document.createElement("div");
    lista.className = "combo-lista";
    box.appendChild(inp); box.appendChild(lista);
    orig.parentNode.insertBefore(box, orig);
    orig.style.display = "none";
    orig._comboInput = inp;
    return { box: box, inp: inp, lista: lista };
  }

  function montarLista(c, itens, escolher) {
    c.lista.innerHTML = "";
    c.ativo = -1;
    itens.slice(0, 200).forEach(function (it, i) {
      var d = document.createElement("div");
      d.className = "combo-item"; d.textContent = it.rotulo; d.dataset.i = i;
      d.addEventListener("mousedown", function (e) { e.preventDefault(); escolher(it); });
      c.lista.appendChild(d);
    });
    if (!itens.length) {
      var v = document.createElement("div"); v.className = "combo-vazio"; v.textContent = "Nada encontrado";
      c.lista.appendChild(v);
    }
    c.lista.style.display = "block";
    c.itens = itens;
  }

  function teclado(c, escolher) {
    c.inp.addEventListener("keydown", function (e) {
      var els = c.lista.querySelectorAll(".combo-item");
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (!els.length) return;
        c.ativo = Math.max(0, Math.min(els.length - 1, (c.ativo == null ? -1 : c.ativo) + (e.key === "ArrowDown" ? 1 : -1)));
        els.forEach(function (x) { x.classList.remove("ativo"); });
        els[c.ativo].classList.add("ativo");
        els[c.ativo].scrollIntoView({ block: "nearest" });
      } else if (e.key === "Enter") {
        if (c.lista.style.display === "block" && c.itens && c.itens.length) {
          e.preventDefault();
          escolher(c.itens[c.ativo >= 0 ? c.ativo : 0]);
        }
      } else if (e.key === "Escape") {
        c.lista.style.display = "none";
      }
    });
    c.inp.addEventListener("blur", function () { setTimeout(function () { c.lista.style.display = "none"; }, 150); });
  }

  function comboSelect(sel) {
    if (sel._comboInput) return;
    var atual = sel.options[sel.selectedIndex];
    var c = criarCaixa(sel, atual && atual.value ? atual.text : "", sel.dataset.placeholder);
    function opcoes() {
      return Array.prototype.filter.call(sel.options, function (o) { return o.value !== "" || o.text.trim(); })
        .map(function (o) { return { codigo: o.value, rotulo: o.text }; });
    }
    function escolher(it) {
      sel.value = it.codigo; c.inp.value = it.codigo ? it.rotulo : "";
      c.lista.style.display = "none";
      sel.dispatchEvent(new Event("change", { bubbles: true }));
    }
    function filtrar(mostrarTudo) {
      var t = semAcento(c.inp.value).trim();
      var itens = opcoes().filter(function (it) { return mostrarTudo || !t || semAcento(it.rotulo).indexOf(t) >= 0; });
      montarLista(c, itens, escolher);
    }
    c.inp.addEventListener("input", function () { filtrar(false); });
    c.inp.addEventListener("focus", function () { c.inp.select(); filtrar(true); });
    teclado(c, escolher);
    sel._comboAtualizar = function () {
      var o = sel.options[sel.selectedIndex];
      c.inp.value = o && o.value ? o.text : "";
    };
    sel._comboHabilitar = function (sim) { c.inp.disabled = !sim; };
    c.inp.disabled = sel.disabled;
  }

  function comboApi(hid) {
    if (hid._comboInput) return;
    var c = criarCaixa(hid, hid.dataset.rotulo || "", hid.dataset.placeholder);
    var timer = null, seq = 0;
    function escolher(it) {
      hid.value = it.codigo; c.inp.value = it.rotulo; hid.dataset.rotulo = it.rotulo;
      c.lista.style.display = "none";
      if (hid.dataset.desc && hid.form) {
        var alvo = hid.form.querySelector('[name="' + hid.dataset.desc + '"]');
        if (alvo) alvo.value = (it.rotulo.split(" - ").slice(1).join(" - ") || "");
      }
      hid.dispatchEvent(new Event("change", { bubbles: true }));
    }
    function buscar() {
      var q = c.inp.value.trim();
      if (q.length < 2) { c.lista.style.display = "none"; return; }
      var meu = ++seq;
      var url = hid.dataset.api + (hid.dataset.api.indexOf("?") >= 0 ? "&" : "?") + "q=" + encodeURIComponent(q);
      fetch(url, { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (dados) {
        if (meu !== seq) return;
        montarLista(c, dados.map(function (x) { return { codigo: x.codigo, rotulo: x.rotulo || x.nome }; }), escolher);
      }).catch(function () {});
    }
    c.inp.addEventListener("input", function () {
      hid.value = ""; clearTimeout(timer); timer = setTimeout(buscar, 200);
    });
    c.inp.addEventListener("focus", function () { c.inp.select(); });
    teclado(c, escolher);
    hid._comboHabilitar = function (sim) { c.inp.disabled = !sim; };
  }

  function filtroTabela(inp) {
    var tab = document.getElementById(inp.dataset.filtraTabela);
    if (!tab) return;
    inp.addEventListener("input", function () {
      var t = semAcento(inp.value).trim();
      Array.prototype.forEach.call(tab.querySelectorAll("tr"), function (tr, i) {
        if (tr.querySelector("th") || tr.classList.contains("total")) return;
        tr.style.display = !t || semAcento(tr.textContent).indexOf(t) >= 0 ? "" : "none";
      });
    });
  }

  window.iniciarCombos = function (raiz) {
    raiz = raiz || document;
    raiz.querySelectorAll("select.combo").forEach(comboSelect);
    raiz.querySelectorAll("input.combo-api").forEach(comboApi);
    raiz.querySelectorAll("input[data-filtra-tabela]").forEach(filtroTabela);
  };
  document.addEventListener("DOMContentLoaded", function () { window.iniciarCombos(); });
})();

// Formulários com class="filtro-auto": aplica o filtro ao escolher uma opção, sem botão.
document.addEventListener("change", function (e) {
  var f = e.target.form;
  if (!f || !f.classList.contains("filtro-auto")) return;
  if (e.target.tagName === "INPUT" && (e.target.type === "text" || e.target.type === "search")) return;
  f.submit();
});

// Campos de valor (class="dinheiro"): ao sair do campo, mostra com duas casas e separador de milhar (2530 -> 2.530,00).
(function () {
  function paraNumero(v) {
    v = (v || "").toString().trim().replace(/^R\$\s*/, "").replace(/\s/g, "");
    if (!v) return null;
    if (v.indexOf(",") >= 0) v = v.replace(/\./g, "").replace(",", ".");
    else if (/^\d{1,3}(\.\d{3})+$/.test(v)) v = v.replace(/\./g, "");   // 2.530 = dois mil e quinhentos e trinta
    var n = Number(v);
    return isFinite(n) ? n : null;
  }
  function formatar(el) {
    var n = paraNumero(el.value);
    if (n === null) return;
    var novo = n.toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (novo !== el.value) {
      el.value = novo;
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    }
  }
  // alíquotas e totais aproximados podem ser percentuais com mais casas: não são formatados
  var SELETOR = "input.dinheiro:not(#aliquota):not([name^=tot_trib_])";
  document.addEventListener("focusout", function (e) {
    if (e.target.matches && e.target.matches(SELETOR)) formatar(e.target);
  });
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(SELETOR).forEach(function (el) { if (el.value) formatar(el); });
  });
  window.formatarDinheiro = formatar;
})();

/* Interruptor: mostra "Ligado/Desligado" ao lado e, com data-auto, salva sozinho (envia o formulário). */
(function () {
  function preparar(el) {
    if (el.dataset.prep) return;
    el.dataset.prep = "1";
    var txt = document.createElement("span");
    txt.className = "estado-interruptor";
    el.insertAdjacentElement("afterend", txt);
    function marcar() { txt.textContent = el.checked ? "Ligado" : "Desligado"; txt.classList.toggle("ligado", el.checked); }
    marcar();
    el.addEventListener("change", function () {
      marcar();
      if (el.hasAttribute("data-auto") && el.form) {
        txt.textContent = "Salvando…";
        if (el.form.requestSubmit) el.form.requestSubmit(); else el.form.submit();
      }
    });
  }
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("input.interruptor").forEach(preparar);
  });
})();
