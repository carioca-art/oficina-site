"""Sistema da Oficina - ordens de servico, clientes, pecas e pagamentos."""
import io
import os
import secrets
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from flask import (Flask, abort, g, jsonify, redirect, render_template,
                   request, send_file, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import fotos
from pdf_os import gerar_pdf

PASTA = os.path.dirname(os.path.abspath(__file__))
PASTA_DADOS = os.environ.get("OFICINA_DADOS", PASTA)
os.makedirs(PASTA_DADOS, exist_ok=True)
BANCO = os.path.join(PASTA_DADOS, "oficina.db")
PASTA_BACKUP = os.path.join(PASTA_DADOS, "backups")
ARQ_CHAVE = os.path.join(PASTA_DADOS, "chave_secreta.txt")

MAX_TENTATIVAS = 5
MINUTOS_BLOQUEIO = 5
FORMAS = ["Dinheiro", "PIX", "Cartão", "Fiado"]
FUSO = ZoneInfo("America/Sao_Paulo")


def agora():
    """Hora de Brasília, mesmo com o servidor em outro fuso."""
    return datetime.now(FUSO).replace(tzinfo=None)


def chave_secreta():
    if not os.path.exists(ARQ_CHAVE):
        with open(ARQ_CHAVE, "w") as f:
            f.write(secrets.token_hex(32))
    with open(ARQ_CHAVE) as f:
        return f.read().strip()


app = Flask(__name__)
app.secret_key = chave_secreta()
app.permanent_session_lifetime = timedelta(days=30)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("OFICINA_HTTPS") == "1")
_ultimo_backup = {"dia": None}


@app.before_request
def backup_do_dia():
    hoje = agora().date()
    if _ultimo_backup["dia"] != hoje:
        fazer_backup()
        _ultimo_backup["dia"] = hoje


# ---------------------------------------------------------------- banco
def db():
    if "db" not in g:
        g.db = sqlite3.connect(BANCO)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA synchronous = FULL")
    return g.db


@app.teardown_appcontext
def fechar_db(_):
    conexao = g.pop("db", None)
    if conexao:
        conexao.close()


ESQUEMA = """
CREATE TABLE IF NOT EXISTS config (chave TEXT PRIMARY KEY, valor TEXT);
CREATE TABLE IF NOT EXISTS clientes (
    id INTEGER PRIMARY KEY, nome TEXT NOT NULL, telefone TEXT DEFAULT '',
    endereco TEXT DEFAULT '', observacoes TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS veiculos (
    id INTEGER PRIMARY KEY, cliente_id INTEGER NOT NULL REFERENCES clientes(id),
    marca TEXT DEFAULT '', modelo TEXT DEFAULT '', ano TEXT DEFAULT '', placa TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS ordens (
    id INTEGER PRIMARY KEY, cliente_id INTEGER NOT NULL REFERENCES clientes(id),
    veiculo_id INTEGER REFERENCES veiculos(id), data TEXT NOT NULL,
    defeito TEXT DEFAULT '', servico TEXT DEFAULT '', mao_de_obra REAL DEFAULT 0,
    status TEXT DEFAULT 'PENDENTE', valor_pago REAL DEFAULT 0,
    forma_pagamento TEXT DEFAULT '', data_pagamento TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS itens (
    id INTEGER PRIMARY KEY, ordem_id INTEGER NOT NULL REFERENCES ordens(id) ON DELETE CASCADE,
    descricao TEXT NOT NULL, quantidade REAL DEFAULT 1, preco REAL DEFAULT 0);
"""


def iniciar_banco():
    conexao = sqlite3.connect(BANCO)
    conexao.execute("PRAGMA journal_mode=WAL")
    conexao.executescript(ESQUEMA)
    conexao.executescript(fotos.ESQUEMA_FOTOS)
    conexao.commit()
    conexao.close()


def copiar_banco(destino):
    """Copia segura, mesmo com o sistema em uso (API de backup do SQLite)."""
    origem = sqlite3.connect(BANCO)
    copia = sqlite3.connect(destino)
    with copia:
        origem.backup(copia)
    copia.close()
    origem.close()


def fazer_backup():
    """Copia o banco uma vez por dia e guarda os ultimos 60."""
    if not os.path.exists(BANCO):
        return
    os.makedirs(PASTA_BACKUP, exist_ok=True)
    destino = os.path.join(PASTA_BACKUP, f"oficina_{agora():%Y-%m-%d}.db")
    if not os.path.exists(destino):
        copiar_banco(destino)
    copias = sorted(f for f in os.listdir(PASTA_BACKUP) if f.endswith(".db"))
    for velha in copias[:-60]:
        os.remove(os.path.join(PASTA_BACKUP, velha))


def cfg(chave, padrao=""):
    linha = db().execute("SELECT valor FROM config WHERE chave=?", (chave,)).fetchone()
    return linha["valor"] if linha else padrao


def salvar_cfg(chave, valor):
    db().execute("INSERT OR REPLACE INTO config (chave, valor) VALUES (?, ?)", (chave, str(valor)))
    db().commit()


CAMPOS_OFICINA = ["oficina_nome", "oficina_endereco",
                  "oficina_tel1_nome", "oficina_telefone", "oficina_tel2_nome", "oficina_telefone2"]


def dados_oficina():
    return {campo: cfg(campo) for campo in CAMPOS_OFICINA}


def salvar_dados_oficina(form):
    for campo in CAMPOS_OFICINA:
        salvar_cfg(campo, form.get(campo, "").strip())


# ---------------------------------------------------------------- utilidades
def numero(texto):
    """Aceita '1.250,50', '1250.50', '150' ou 'R$ 80'."""
    texto = (texto or "").replace("R$", "").strip()
    if not texto:
        return 0.0
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    try:
        return round(float(texto), 2)
    except ValueError:
        return 0.0


@app.template_filter("dinheiro")
def dinheiro(valor):
    valor = valor or 0
    texto = f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {texto}"


@app.template_filter("data_br")
def data_br(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%d/%m/%Y")
    except (TypeError, ValueError):
        return ""


@app.template_filter("qtd")
def qtd(valor):
    return f"{valor:g}".replace(".", ",")


def total_os(ordem_id):
    linha = db().execute(
        "SELECT COALESCE(SUM(quantidade*preco),0) AS pecas FROM itens WHERE ordem_id=?",
        (ordem_id,)).fetchone()
    ordem = db().execute("SELECT mao_de_obra FROM ordens WHERE id=?", (ordem_id,)).fetchone()
    return round(linha["pecas"] + (ordem["mao_de_obra"] if ordem else 0), 2)


def carregar_os(ordem_id):
    ordem = db().execute("""
        SELECT o.*, c.nome, c.telefone, c.endereco,
               v.marca, v.modelo, v.ano, v.placa
        FROM ordens o JOIN clientes c ON c.id = o.cliente_id
        LEFT JOIN veiculos v ON v.id = o.veiculo_id WHERE o.id=?""", (ordem_id,)).fetchone()
    if not ordem:
        abort(404)
    itens = db().execute("SELECT * FROM itens WHERE ordem_id=? ORDER BY id", (ordem_id,)).fetchall()
    ordem = dict(ordem)
    ordem["itens"] = [dict(i) for i in itens]
    ordem["total"] = total_os(ordem_id)
    ordem["falta"] = max(ordem["total"] - (ordem["valor_pago"] or 0), 0) if ordem["status"] != "PAGO" else 0
    return ordem


def lista_os(where="", params=()):
    linhas = db().execute(f"""
        SELECT o.id, o.cliente_id, o.data, o.status, o.valor_pago, c.nome, c.telefone,
               v.marca, v.modelo, v.placa
        FROM ordens o JOIN clientes c ON c.id = o.cliente_id
        LEFT JOIN veiculos v ON v.id = o.veiculo_id
        {where} ORDER BY o.data DESC, o.id DESC LIMIT 100""", params).fetchall()
    resultado = []
    for linha in linhas:
        item = dict(linha)
        item["total"] = total_os(item["id"])
        item["falta"] = 0 if item["status"] == "PAGO" else max(item["total"] - (item["valor_pago"] or 0), 0)
        resultado.append(item)
    return resultado


# ---------------------------------------------------------------- acesso
def precisa_entrar(funcao):
    @wraps(funcao)
    def verificar(*args, **kwargs):
        if not cfg("senha"):
            return redirect(url_for("primeiro_acesso"))
        if not session.get("dentro"):
            return redirect(url_for("entrar"))
        return funcao(*args, **kwargs)
    return verificar


@app.context_processor
def dados_globais():
    try:
        return {"oficina_nome": cfg("oficina_nome", "Oficina")}
    except sqlite3.Error:
        return {"oficina_nome": "Oficina"}


@app.route("/primeiro-acesso", methods=["GET", "POST"])
def primeiro_acesso():
    if cfg("senha"):
        return redirect(url_for("entrar"))
    erro = ""
    if request.method == "POST":
        nome = request.form.get("oficina_nome", "").strip()
        senha = request.form.get("senha", "")
        repetir = request.form.get("repetir", "")
        codigo = os.environ.get("OFICINA_CODIGO", "")
        if codigo and request.form.get("codigo", "").strip() != codigo:
            erro = "Código de liberação errado."
        elif not nome:
            erro = "Escreva o nome da oficina."
        elif not (senha.isdigit() and len(senha) == 4):
            erro = "A senha precisa ter 4 números."
        elif senha != repetir:
            erro = "As duas senhas ficaram diferentes. Digite de novo."
        else:
            salvar_dados_oficina(request.form)
            salvar_cfg("senha", generate_password_hash(senha))
            session.permanent = True
            session["dentro"] = True
            return redirect(url_for("inicio"))
    return render_template("primeiro_acesso.html", erro=erro,
                           pede_codigo=bool(os.environ.get("OFICINA_CODIGO")))


@app.route("/entrar", methods=["GET", "POST"])
def entrar():
    if not cfg("senha"):
        return redirect(url_for("primeiro_acesso"))
    erro = ""
    bloqueado_ate = cfg("bloqueado_ate")
    if bloqueado_ate and datetime.fromisoformat(bloqueado_ate) > agora():
        minutos = int((datetime.fromisoformat(bloqueado_ate) - agora()).total_seconds() // 60) + 1
        return render_template("entrar.html", bloqueado=minutos)
    if request.method == "POST":
        if check_password_hash(cfg("senha"), request.form.get("senha", "")):
            salvar_cfg("tentativas", 0)
            session.permanent = True
            session["dentro"] = True
            return redirect(url_for("inicio"))
        tentativas = int(cfg("tentativas", "0")) + 1
        if tentativas >= MAX_TENTATIVAS:
            salvar_cfg("tentativas", 0)
            salvar_cfg("bloqueado_ate", (agora() + timedelta(minutes=MINUTOS_BLOQUEIO)).isoformat())
            return render_template("entrar.html", bloqueado=MINUTOS_BLOQUEIO)
        salvar_cfg("tentativas", tentativas)
        restam = MAX_TENTATIVAS - tentativas
        erro = f"Senha errada. Você ainda pode tentar {restam} {'vez' if restam == 1 else 'vezes'}."
    return render_template("entrar.html", erro=erro)


@app.route("/sair")
def sair():
    session.clear()
    return redirect(url_for("entrar"))


# ---------------------------------------------------------------- telas
@app.route("/")
@precisa_entrar
def inicio():
    pendentes = lista_os("WHERE o.status != 'PAGO'")
    return render_template("inicio.html",
                           qtd_receber=len(pendentes),
                           valor_receber=sum(o["falta"] for o in pendentes))


@app.route("/os/nova")
@precisa_entrar
def nova_os():
    vazia = {"id": None, "data": agora().date().isoformat(), "nome": "", "telefone": "",
             "endereco": "", "cliente_id": "", "veiculo_id": "", "marca": "", "modelo": "",
             "ano": "", "placa": "", "defeito": "", "servico": "", "mao_de_obra": 0,
             "status": "PENDENTE", "valor_pago": 0, "forma_pagamento": "", "itens": []}
    cliente = db().execute("SELECT * FROM clientes WHERE id=?",
                           (request.args.get("cliente", type=int),)).fetchone()
    if cliente:
        vazia.update(cliente_id=cliente["id"], nome=cliente["nome"],
                     telefone=cliente["telefone"], endereco=cliente["endereco"])
    return render_template("os_form.html", os=vazia, formas=FORMAS)


@app.route("/os/<int:ordem_id>/editar")
@precisa_entrar
def editar_os(ordem_id):
    return render_template("os_form.html", os=carregar_os(ordem_id), formas=FORMAS)


@app.route("/os/salvar", methods=["POST"])
@precisa_entrar
def salvar_os():
    f = request.form
    con = db()
    nome = f.get("nome", "").strip()
    if not nome:
        return "Escreva o nome do cliente.", 400

    # cliente: usa o escolhido na lista ou cria um novo
    cliente_id = f.get("cliente_id") or None
    if cliente_id:
        con.execute("UPDATE clientes SET nome=?, telefone=?, endereco=? WHERE id=?",
                    (nome, f.get("telefone", "").strip(), f.get("endereco", "").strip(), cliente_id))
    else:
        cliente_id = con.execute("INSERT INTO clientes (nome, telefone, endereco) VALUES (?,?,?)",
                                 (nome, f.get("telefone", "").strip(), f.get("endereco", "").strip())).lastrowid

    # veiculo: escolhido, encontrado pela placa, ou novo
    campos_v = {k: f.get(k, "").strip() for k in ("marca", "modelo", "ano", "placa")}
    campos_v["placa"] = campos_v["placa"].upper()
    veiculo_id = f.get("veiculo_id") or None
    if not veiculo_id and campos_v["placa"]:
        achado = con.execute("SELECT id FROM veiculos WHERE cliente_id=? AND placa=?",
                             (cliente_id, campos_v["placa"])).fetchone()
        veiculo_id = achado["id"] if achado else None
    if veiculo_id:
        con.execute("UPDATE veiculos SET marca=?, modelo=?, ano=?, placa=? WHERE id=?",
                    (*campos_v.values(), veiculo_id))
    elif any(campos_v.values()):
        veiculo_id = con.execute("INSERT INTO veiculos (cliente_id, marca, modelo, ano, placa) VALUES (?,?,?,?,?)",
                                 (cliente_id, *campos_v.values())).lastrowid

    status = f.get("status", "PENDENTE")
    if status not in ("PAGO", "PENDENTE", "PARCIAL"):
        status = "PENDENTE"
    if status == "PARCIAL" and numero(f.get("valor_pago")) <= 0:
        status = "PENDENTE"
    total_form = numero(f.get("mao_de_obra")) + sum(
        (numero(q) or 1) * numero(pr) for d, q, pr in zip(f.getlist("item_descricao"),
                                                          f.getlist("item_quantidade"),
                                                          f.getlist("item_preco")) if d.strip())
    if status == "PARCIAL" and numero(f.get("valor_pago")) >= total_form > 0:
        status = "PAGO"
    dados = (cliente_id, veiculo_id, f.get("data") or agora().date().isoformat(),
             f.get("defeito", "").strip(), f.get("servico", "").strip(), numero(f.get("mao_de_obra")),
             status, numero(f.get("valor_pago")) if status == "PARCIAL" else 0,
             f.get("forma_pagamento", "") if status != "PENDENTE" else "",
             agora().date().isoformat() if status != "PENDENTE" else "")

    ordem_id = f.get("id") or None
    if ordem_id:
        con.execute("""UPDATE ordens SET cliente_id=?, veiculo_id=?, data=?, defeito=?, servico=?,
                       mao_de_obra=?, status=?, valor_pago=?, forma_pagamento=?, data_pagamento=?
                       WHERE id=?""", (*dados, ordem_id))
    else:
        ordem_id = con.execute("""INSERT INTO ordens (cliente_id, veiculo_id, data, defeito, servico,
                       mao_de_obra, status, valor_pago, forma_pagamento, data_pagamento)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""", dados).lastrowid

    existentes = {linha["id"] for linha in con.execute("SELECT id FROM itens WHERE ordem_id=?", (ordem_id,))}
    mantidos = set()
    ids_form = f.getlist("item_id")
    linhas_form = zip(f.getlist("item_descricao"), f.getlist("item_quantidade"), f.getlist("item_preco"))
    for posicao, (descricao, quantidade, preco) in enumerate(linhas_form):
        if not descricao.strip():
            continue
        valores = (descricao.strip(), numero(quantidade) or 1, numero(preco))
        item_id = int(ids_form[posicao]) if posicao < len(ids_form) and ids_form[posicao].isdigit() else None
        if item_id in existentes:
            con.execute("UPDATE itens SET descricao=?, quantidade=?, preco=? WHERE id=?", (*valores, item_id))
            mantidos.add(item_id)
        else:
            con.execute("INSERT INTO itens (ordem_id, descricao, quantidade, preco) VALUES (?,?,?,?)",
                        (ordem_id, *valores))
    for removido in existentes - mantidos:
        con.execute("UPDATE fotos SET item_id=NULL WHERE item_id=?", (removido,))
        con.execute("DELETE FROM itens WHERE id=?", (removido,))
    con.commit()
    return redirect(url_for("ver_os", ordem_id=ordem_id, salvo=1))


@app.route("/os/<int:ordem_id>")
@precisa_entrar
def ver_os(ordem_id):
    return render_template("os_ver.html", os=carregar_os(ordem_id), salvo=request.args.get("salvo"),
                           aviso=request.args.get("aviso"), formas=FORMAS,
                           fotos=FOTOS["fotos_da_os"](ordem_id))


@app.route("/os/<int:ordem_id>/pagamento", methods=["POST"])
@precisa_entrar
def registrar_pagamento(ordem_id):
    ordem = carregar_os(ordem_id)
    forma = request.form.get("forma_pagamento", "Dinheiro")
    hoje = agora().date().isoformat()
    if request.form.get("tipo") == "parte":
        valor = numero(request.form.get("valor"))
        if valor <= 0:
            return redirect(url_for("ver_os", ordem_id=ordem_id, aviso="valor"))
        pago = round((ordem["valor_pago"] or 0) + valor, 2)
        status = "PAGO" if pago >= ordem["total"] else "PARCIAL"
        db().execute("UPDATE ordens SET status=?, valor_pago=?, forma_pagamento=?, data_pagamento=? WHERE id=?",
                     (status, pago, forma, hoje, ordem_id))
    else:
        db().execute("UPDATE ordens SET status='PAGO', valor_pago=?, forma_pagamento=?, data_pagamento=? WHERE id=?",
                     (ordem["total"], forma, hoje, ordem_id))
    db().commit()
    return redirect(url_for("ver_os", ordem_id=ordem_id, aviso="pago"))


@app.route("/os/<int:ordem_id>/apagar", methods=["GET", "POST"])
@precisa_entrar
def apagar_os(ordem_id):
    ordem = carregar_os(ordem_id)
    if request.method == "POST":
        FOTOS["apagar_fotos_da_os"](ordem_id)
        db().execute("DELETE FROM itens WHERE ordem_id=?", (ordem_id,))
        db().execute("DELETE FROM ordens WHERE id=?", (ordem_id,))
        db().commit()
        return redirect(url_for("inicio"))
    return render_template("apagar.html", os=ordem)


@app.route("/os/<int:ordem_id>/pdf")
@precisa_entrar
def pdf_os(ordem_id):
    ordem = carregar_os(ordem_id)
    nomes_itens = {i["id"]: i["descricao"] for i in ordem["itens"]}
    lista_fotos = []
    for foto in sorted(FOTOS["fotos_da_os"](ordem_id), key=lambda x: (x["tipo"] != "veiculo", x["item_id"] or 0, x["id"])):
        legenda = fotos.TIPOS[foto["tipo"]]
        if foto["item_id"] in nomes_itens:
            legenda = f"{nomes_itens[foto['item_id']]} ({legenda.lower()})"
        lista_fotos.append({"legenda": legenda, "arquivo": FOTOS["arquivo_para_pdf"](foto["id"]),
                            "problema": foto["problema"]})
    arquivo = gerar_pdf(ordem, dados_oficina(), lista_fotos)
    nome = f"OS-{ordem_id:04d}-{ordem['nome'].split()[0]}.pdf"
    return send_file(arquivo, mimetype="application/pdf", download_name=nome,
                     as_attachment=request.args.get("baixar") == "1")


@app.route("/buscar")
@precisa_entrar
def buscar():
    termo = request.args.get("q", "").strip()
    resultados = []
    if termo:
        like = f"%{termo}%"
        resultados = lista_os("""WHERE c.nome LIKE ? OR c.telefone LIKE ? OR v.placa LIKE ?
                                 OR v.modelo LIKE ? OR v.marca LIKE ?""",
                              (like, like, like.upper(), like, like))
    else:
        resultados = lista_os()[:20]
    return render_template("buscar.html", termo=termo, resultados=resultados)


@app.route("/receber")
@precisa_entrar
def receber():
    pendentes = lista_os("WHERE o.status != 'PAGO'")
    return render_template("receber.html", resultados=pendentes,
                           total=sum(o["falta"] for o in pendentes))


def resumo_clientes(cliente_id=None):
    filtro = "WHERE c.id = ?" if cliente_id else ""
    linhas = db().execute(f"""
        SELECT c.*, COUNT(o.id) AS qtd_os FROM clientes c
        LEFT JOIN ordens o ON o.cliente_id = c.id
        {filtro} GROUP BY c.id ORDER BY c.nome COLLATE NOCASE""",
        (cliente_id,) if cliente_id else ()).fetchall()
    resultado = []
    for linha in linhas:
        c = dict(linha)
        veiculos = db().execute("SELECT * FROM veiculos WHERE cliente_id=? ORDER BY id", (c["id"],)).fetchall()
        c["veiculos"] = [dict(v) for v in veiculos]
        c["deve"] = sum(o["falta"] for o in lista_os(
            "WHERE o.status != 'PAGO' AND o.cliente_id = ?", (c["id"],)))
        resultado.append(c)
    return resultado


@app.route("/clientes")
@precisa_entrar
def clientes():
    return render_template("clientes.html", clientes=resumo_clientes())


@app.route("/clientes/<int:cliente_id>", methods=["GET", "POST"])
@precisa_entrar
def ver_cliente(cliente_id):
    achados = resumo_clientes(cliente_id)
    if not achados:
        abort(404)
    if request.method == "POST":
        nome = request.form.get("nome", "").strip()
        if nome:
            db().execute("UPDATE clientes SET nome=?, telefone=?, endereco=?, observacoes=? WHERE id=?",
                         (nome, request.form.get("telefone", "").strip(),
                          request.form.get("endereco", "").strip(),
                          request.form.get("observacoes", "").strip(), cliente_id))
            db().commit()
        return redirect(url_for("ver_cliente", cliente_id=cliente_id, salvo=1))
    return render_template("cliente.html", c=achados[0],
                           ordens=lista_os("WHERE o.cliente_id = ?", (cliente_id,)),
                           editar=request.args.get("editar"), salvo=request.args.get("salvo"))


@app.route("/oficina", methods=["GET", "POST"])
@precisa_entrar
def oficina():
    if request.method == "POST":
        if not request.form.get("oficina_nome", "").strip():
            return render_template("oficina.html", o=request.form, erro="Escreva o nome da oficina.")
        salvar_dados_oficina(request.form)
        return render_template("oficina.html", o=dados_oficina(), salvo=True)
    return render_template("oficina.html", o=dados_oficina())


@app.route("/api/clientes")
@precisa_entrar
def api_clientes():
    termo = request.args.get("q", "").strip()
    if len(termo) < 2:
        return jsonify([])
    clientes = db().execute("SELECT * FROM clientes WHERE nome LIKE ? ORDER BY nome LIMIT 6",
                            (f"%{termo}%",)).fetchall()
    resposta = []
    for c in clientes:
        veiculos = db().execute("SELECT * FROM veiculos WHERE cliente_id=?", (c["id"],)).fetchall()
        resposta.append({**dict(c), "veiculos": [dict(v) for v in veiculos]})
    return jsonify(resposta)


@app.route("/copia-de-seguranca")
@precisa_entrar
def baixar_copia():
    """Banco de dados + todas as fotos num arquivo .zip."""
    pasta_temp = tempfile.mkdtemp()
    banco_temp = os.path.join(pasta_temp, "oficina.db")
    copiar_banco(banco_temp)
    zip_temp = os.path.join(pasta_temp, "copia.zip")
    with zipfile.ZipFile(zip_temp, "w", zipfile.ZIP_DEFLATED) as pacote:
        pacote.write(banco_temp, "oficina.db")
        for nome in sorted(os.listdir(FOTOS["pasta"])):
            if not nome.endswith("_mini.jpg"):
                pacote.write(os.path.join(FOTOS["pasta"], nome), f"fotos/{nome}")
    with open(zip_temp, "rb") as f:
        dados = io.BytesIO(f.read())
    for nome in os.listdir(pasta_temp):
        os.remove(os.path.join(pasta_temp, nome))
    os.rmdir(pasta_temp)
    return send_file(dados, mimetype="application/zip", as_attachment=True,
                     download_name=f"oficina_copia_{agora():%Y-%m-%d}.zip")


@app.route("/manifest.json")
def manifest():
    return jsonify({"name": "Oficina", "short_name": "Oficina", "start_url": "/",
                    "display": "standalone", "background_color": "#ffffff", "theme_color": "#1a1a1a",
                    "lang": "pt-BR"})


iniciar_banco()
fazer_backup()
FOTOS = fotos.registrar(app, db, precisa_entrar, carregar_os, agora, PASTA_DADOS)

if __name__ == "__main__":
    # host 0.0.0.0 deixa o celular na mesma rede Wi-Fi acessar
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
