"""Fotos do caminhão e das peças, com marcações desenhadas por cima."""
import io
import json
import os

from flask import abort, jsonify, redirect, render_template, request, send_file, url_for
from PIL import Image, ImageOps

TIPOS = {"veiculo": "Caminhão", "antiga": "Peça antiga", "comprada": "Peça comprada"}
LADO_MAXIMO = 1600
LADO_MINI = 420

ESQUEMA_FOTOS = """
CREATE TABLE IF NOT EXISTS fotos (
    id INTEGER PRIMARY KEY, ordem_id INTEGER NOT NULL REFERENCES ordens(id),
    item_id INTEGER REFERENCES itens(id), tipo TEXT NOT NULL,
    problema TEXT DEFAULT '', anotacoes TEXT DEFAULT '[]', marcada INTEGER DEFAULT 0,
    criada TEXT DEFAULT '');
"""


def registrar(app, db, precisa_entrar, carregar_os, agora, pasta_dados):
    pasta = os.path.join(pasta_dados, "fotos")
    os.makedirs(pasta, exist_ok=True)

    def caminho(foto_id, versao=""):
        return os.path.join(pasta, f"{foto_id}{versao}.jpg")

    def gravar_imagem(arquivo, destino, girar=True):
        """Abre, endireita, diminui e salva como JPEG. Recusa o que não for imagem."""
        try:
            imagem = Image.open(arquivo)
            if girar:
                imagem = ImageOps.exif_transpose(imagem)
            imagem = imagem.convert("RGB")
        except Exception:
            return False
        imagem.thumbnail((LADO_MAXIMO, LADO_MAXIMO))
        imagem.save(destino, "JPEG", quality=82, optimize=True)
        return True

    def gerar_mini(foto_id):
        base = caminho(foto_id, "_marcada") if os.path.exists(caminho(foto_id, "_marcada")) else caminho(foto_id)
        imagem = Image.open(base)
        imagem.thumbnail((LADO_MINI, LADO_MINI))
        imagem.save(caminho(foto_id, "_mini"), "JPEG", quality=78)

    def pegar_foto(foto_id):
        foto = db().execute("SELECT * FROM fotos WHERE id=?", (foto_id,)).fetchone()
        if not foto:
            abort(404)
        return dict(foto)

    def apagar_arquivos(foto_id):
        for versao in ("", "_marcada", "_mini"):
            if os.path.exists(caminho(foto_id, versao)):
                os.remove(caminho(foto_id, versao))

    @app.route("/os/<int:ordem_id>/fotos", methods=["POST"])
    @precisa_entrar
    def enviar_foto(ordem_id):
        carregar_os(ordem_id)
        tipo = request.form.get("tipo")
        if tipo not in TIPOS or "foto" not in request.files:
            return jsonify(erro="Foto inválida."), 400
        item_id = request.form.get("item_id", type=int) if tipo != "veiculo" else None
        if item_id and not db().execute("SELECT 1 FROM itens WHERE id=? AND ordem_id=?",
                                        (item_id, ordem_id)).fetchone():
            item_id = None
        foto_id = db().execute(
            "INSERT INTO fotos (ordem_id, item_id, tipo, criada) VALUES (?,?,?,?)",
            (ordem_id, item_id, tipo, agora().isoformat(timespec="seconds"))).lastrowid
        if not gravar_imagem(request.files["foto"], caminho(foto_id)):
            db().rollback()
            return jsonify(erro="Esse arquivo não é uma foto."), 400
        gerar_mini(foto_id)
        db().commit()
        return jsonify(id=foto_id, editar=url_for("editar_foto", foto_id=foto_id))

    @app.route("/fotos/<int:foto_id>.jpg")
    @precisa_entrar
    def ver_foto(foto_id):
        pegar_foto(foto_id)
        if request.args.get("mini") and os.path.exists(caminho(foto_id, "_mini")):
            arquivo = caminho(foto_id, "_mini")
        elif not request.args.get("original") and os.path.exists(caminho(foto_id, "_marcada")):
            arquivo = caminho(foto_id, "_marcada")
        else:
            arquivo = caminho(foto_id)
        if not os.path.exists(arquivo):
            abort(404)
        resposta = send_file(arquivo, mimetype="image/jpeg")
        resposta.headers["Cache-Control"] = "no-cache"
        return resposta

    @app.route("/fotos/<int:foto_id>", methods=["GET", "POST"])
    @precisa_entrar
    def editar_foto(foto_id):
        foto = pegar_foto(foto_id)
        if request.method == "POST":
            anotacoes = request.form.get("anotacoes", "[]")
            try:
                marcas = json.loads(anotacoes)
                assert isinstance(marcas, list)
            except (ValueError, AssertionError):
                marcas = []
            marcada = 0
            if marcas and "marcada" in request.files:
                marcada = int(gravar_imagem(request.files["marcada"], caminho(foto_id, "_marcada"), girar=False))
            if not marcada and os.path.exists(caminho(foto_id, "_marcada")):
                os.remove(caminho(foto_id, "_marcada"))
            db().execute("UPDATE fotos SET problema=?, anotacoes=?, marcada=? WHERE id=?",
                         (request.form.get("problema", "").strip(), json.dumps(marcas), marcada, foto_id))
            db().commit()
            gerar_mini(foto_id)
            return jsonify(voltar=url_for("ver_os", ordem_id=foto["ordem_id"], aviso="foto") + "#fotos")
        item = None
        if foto["item_id"]:
            item = db().execute("SELECT * FROM itens WHERE id=?", (foto["item_id"],)).fetchone()
        return render_template("foto.html", foto=foto, item=item, tipo_nome=TIPOS[foto["tipo"]])

    @app.route("/fotos/<int:foto_id>/apagar", methods=["POST"])
    @precisa_entrar
    def apagar_foto(foto_id):
        foto = pegar_foto(foto_id)
        db().execute("DELETE FROM fotos WHERE id=?", (foto_id,))
        db().commit()
        apagar_arquivos(foto_id)
        return redirect(url_for("ver_os", ordem_id=foto["ordem_id"]) + "#fotos")

    def fotos_da_os(ordem_id):
        return [dict(f) for f in db().execute(
            "SELECT * FROM fotos WHERE ordem_id=? ORDER BY id", (ordem_id,)).fetchall()]

    def arquivo_para_pdf(foto_id):
        marcada = caminho(foto_id, "_marcada")
        return marcada if os.path.exists(marcada) else caminho(foto_id)

    def apagar_fotos_da_os(ordem_id):
        for foto in fotos_da_os(ordem_id):
            apagar_arquivos(foto["id"])
        db().execute("DELETE FROM fotos WHERE ordem_id=?", (ordem_id,))

    return {"fotos_da_os": fotos_da_os, "arquivo_para_pdf": arquivo_para_pdf,
            "apagar_fotos_da_os": apagar_fotos_da_os, "pasta": pasta}
