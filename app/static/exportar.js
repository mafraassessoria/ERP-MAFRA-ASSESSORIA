/* Exportar ZIP do filtro: acrescenta PDF/XML escolhidos ao link. */
document.addEventListener("click", function (ev) {
  var a = ev.target.closest && ev.target.closest("a.exportar-zip");
  if (!a) return;
  var caixa = a.closest(".export-filtro");
  var pdf = caixa.querySelector(".exp-pdf").checked, xml = caixa.querySelector(".exp-xml").checked;
  if (!pdf && !xml) { ev.preventDefault(); alert("Marque PDF, XML ou os dois."); return; }
  var url = a.getAttribute("href").replace(/&?(pdf|xml)=1/g, "");
  url += (url.indexOf("?") >= 0 ? "&" : "?") + (pdf ? "pdf=1" : "") + (pdf && xml ? "&" : "") + (xml ? "xml=1" : "");
  a.setAttribute("href", url);
  var txt = a.textContent;
  a.textContent = "Gerando ZIP... aguarde";
  setTimeout(function () { a.textContent = txt; }, 8000);
});
