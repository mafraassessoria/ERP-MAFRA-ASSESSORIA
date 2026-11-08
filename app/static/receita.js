// Consulta em lote: mostra só os campos do serviço escolhido e a lista de empresas quando "marcadas".
(function () {
  const sel = document.getElementById('lote-servico');
  if (!sel) return;
  function campos() {
    document.querySelectorAll('.campo-lote').forEach(function (el) {
      const ativo = el.dataset.servico === sel.value;
      el.style.display = ativo ? '' : 'none';
      el.querySelectorAll('input,select').forEach(function (i) { i.disabled = !ativo; });
    });
  }
  sel.addEventListener('change', campos); campos();
  const emp = document.getElementById('lote-empresas'), lista = document.getElementById('lote-lista');
  function mostra() { lista.classList.toggle('oculto', emp.value !== 'marcadas'); if (emp.value === 'marcadas') lista.open = true; }
  emp.addEventListener('change', mostra); mostra();
  const todas = document.getElementById('marcar-todas');
  if (todas) todas.addEventListener('change', function () {
    document.querySelectorAll('.lote-id').forEach(function (c) { c.checked = todas.checked; });
  });
})();
