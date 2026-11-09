// Ordena as tabelas ao clicar no título da coluna (todas que têm cabeçalho <th>; class="sem-ordenar" desliga).
(function () {
  function valor(td) {
    if (!td) return "";
    const t = (td.getAttribute("data-valor") || td.textContent || "").trim();
    let m = t.match(/^(\d{2})\/(\d{2})\/(\d{4})/);               // dd/mm/aaaa
    if (m) return { n: Number(m[3] + m[2] + m[1]) };
    if (/^-?[\d.]+,\d{2}$/.test(t)) return { n: Number(t.replace(/\./g, "").replace(",", ".")) };  // 1.234,56
    if (/^-?\d+$/.test(t)) return { n: Number(t) };
    m = t.match(/^R\$\s*(-?[\d.]+,\d{2})/);                                // R$ 1.234,56
    if (m) return { n: Number(m[1].replace(/\./g, "").replace(",", ".")) };
    m = t.match(/^(\d+)\s*[·-]\s/);                                         // "24 · EMPRESA"
    if (m) return { n: Number(m[1]), s: t.toLocaleLowerCase("pt-BR") };
    return { s: t.toLocaleLowerCase("pt-BR") };
  }
  function comparar(a, b) {
    if (a.n !== undefined && b.n !== undefined) return a.n - b.n;
    if (a.n !== undefined) return -1;
    if (b.n !== undefined) return 1;
    return a.s.localeCompare(b.s, "pt-BR", { numeric: true });
  }
  document.querySelectorAll("table:not(.sem-ordenar)").forEach(function (tabela) {
    const cab = tabela.querySelector("tr");
    if (!cab) return;
    const ths = [...cab.children];
    if (!ths.length || ths.some(x => x.tagName !== "TH") || ths.some(x => x.colSpan > 1)) return;
    if (tabela.querySelectorAll("tr").length < 3) return;
    ths.forEach(function (th, i) {
      if (th.classList.contains("sem-ordem") || th.querySelector("input")) return;
      th.classList.add("clicavel");
      th.title = "Clique para ordenar";
      th.addEventListener("click", function () {
        const asc = th.dataset.dir !== "asc";
        ths.forEach(x => { delete x.dataset.dir; x.classList.remove("asc", "desc"); });
        th.dataset.dir = asc ? "asc" : "desc";
        th.classList.add(asc ? "asc" : "desc");
        const linhas = [...tabela.querySelectorAll("tr")].slice(1).filter(tr => !tr.classList.contains("total") && tr.children.length === ths.length);
        const fixas = [...tabela.querySelectorAll("tr.total")];
        linhas.sort((a, b) => { const r = comparar(valor(a.children[i]), valor(b.children[i])); return asc ? r : -r; });
        const pai = linhas.length ? linhas[0].parentNode : null;
        if (!pai) return;
        linhas.forEach(tr => pai.appendChild(tr));
        fixas.forEach(tr => pai.appendChild(tr));
      });
    });
  });
})();
