// Diminui a foto no próprio celular (sobe rápido e ocupa pouco espaço) e envia.
(function () {
  var script = document.getElementById('script-fotos');
  var endereco = script.dataset.envio;
  var campo = document.getElementById('arquivo-foto');
  var enviando = document.getElementById('enviando');
  var erro = document.getElementById('erro-foto');
  var atual = null;

  function reduzir(arquivo) {
    return new Promise(function (pronto) {
      var img = new Image();
      img.onload = function () {
        var escala = Math.min(1, 1600 / Math.max(img.naturalWidth, img.naturalHeight));
        var tela = document.createElement('canvas');
        tela.width = Math.round(img.naturalWidth * escala);
        tela.height = Math.round(img.naturalHeight * escala);
        tela.getContext('2d').drawImage(img, 0, 0, tela.width, tela.height);
        URL.revokeObjectURL(img.src);
        tela.toBlob(function (b) { pronto(b || arquivo); }, 'image/jpeg', 0.85);
      };
      img.onerror = function () { pronto(arquivo); };
      img.src = URL.createObjectURL(arquivo);
    });
  }

  document.querySelectorAll('.botao-foto').forEach(function (botao) {
    botao.addEventListener('click', function () {
      atual = { tipo: botao.dataset.tipo, item: botao.dataset.item || '' };
      campo.value = '';
      campo.click();
    });
  });

  campo.addEventListener('change', function () {
    var arquivo = campo.files[0];
    if (!arquivo || !atual) return;
    erro.hidden = true;
    enviando.hidden = false;
    enviando.scrollIntoView({ block: 'center' });
    reduzir(arquivo).then(function (foto) {
      var dados = new FormData();
      dados.append('tipo', atual.tipo);
      dados.append('item_id', atual.item);
      dados.append('foto', foto, 'foto.jpg');
      return fetch(endereco, { method: 'POST', body: dados });
    }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok) throw new Error(res.j.erro || 'erro');
        // peça antiga: já abre pra marcar o problema
        if (atual.tipo === 'antiga') { location.href = res.j.editar; }
        else { location.hash = 'fotos'; location.reload(); }
      })
      .catch(function (e) {
        enviando.hidden = true;
        erro.hidden = false;
        erro.textContent = 'Não deu pra enviar a foto. ' + (e.message && e.message !== 'erro' ? e.message : 'Verifique a internet e tente de novo.');
      });
  });
})();
