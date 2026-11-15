"""Gera o PDF da ordem de servico, letra grande e organizado."""
import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from PIL import Image as Imagem
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

CINZA = colors.HexColor("#e6e6e6")


def _r(valor):
    texto = f"{valor or 0:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {texto}"


def _data(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%d/%m/%Y")
    except (TypeError, ValueError):
        return ""


def _esc(texto):
    return (texto or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")


def _imagem(arquivo, largura_max, altura_max):
    """Foto menor (o PDF fica leve pro WhatsApp) e no tamanho certo da página."""
    try:
        foto = Imagem.open(arquivo)
        foto.thumbnail((1000, 1000))
        dados = io.BytesIO()
        foto.convert("RGB").save(dados, "JPEG", quality=75)
    except Exception:
        return None
    dados.seek(0)
    escala = min(largura_max / foto.width, altura_max / foto.height)
    return Image(dados, width=foto.width * escala, height=foto.height * escala)


def gerar_pdf(os_, oficina, fotos=None):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=14 * mm, bottomMargin=14 * mm,
                            title=f"Ordem de serviço {os_['id']:04d}")
    largura = A4[0] - 30 * mm

    normal = ParagraphStyle("n", fontName="Helvetica", fontSize=13, leading=17)
    negrito = ParagraphStyle("b", parent=normal, fontName="Helvetica-Bold")
    titulo = ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=22, leading=26)
    secao = ParagraphStyle("s", fontName="Helvetica-Bold", fontSize=14, leading=18,
                           spaceBefore=10, spaceAfter=4)
    direita = ParagraphStyle("d", parent=normal, alignment=TA_RIGHT)
    direita_b = ParagraphStyle("db", parent=negrito, alignment=TA_RIGHT)

    partes = []

    # cabecalho
    info_oficina = [Paragraph(_esc(oficina.get("oficina_nome")), titulo)]
    for dono, numero in ((oficina.get("oficina_tel1_nome"), oficina.get("oficina_telefone")),
                         (oficina.get("oficina_tel2_nome"), oficina.get("oficina_telefone2"))):
        if numero:
            texto = f"{_esc(dono)}: {_esc(numero)}" if dono else _esc(numero)
            info_oficina.append(Paragraph(texto, normal))
    if oficina.get("oficina_endereco"):
        info_oficina.append(Paragraph(_esc(oficina["oficina_endereco"]), normal))
    info_os = [Paragraph(f"OS nº {os_['id']:04d}", ParagraphStyle("o", parent=titulo, alignment=TA_RIGHT)),
               Paragraph(f"Data: {_data(os_['data'])}", direita)]
    cab = Table([[info_oficina, info_os]], colWidths=[largura * 0.62, largura * 0.38])
    cab.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                             ("LINEBELOW", (0, 0), (-1, 0), 2, colors.black),
                             ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    partes += [cab, Spacer(1, 6)]

    # cliente e veiculo lado a lado
    cliente = [Paragraph("Cliente", secao), Paragraph(_esc(os_["nome"]), negrito)]
    if os_.get("telefone"):
        cliente.append(Paragraph(f"Telefone: {_esc(os_['telefone'])}", normal))
    if os_.get("endereco"):
        cliente.append(Paragraph(_esc(os_["endereco"]), normal))
    veiculo = [Paragraph("Veículo", secao)]
    nome_veiculo = " ".join(x for x in (os_.get("marca"), os_.get("modelo")) if x)
    if nome_veiculo:
        veiculo.append(Paragraph(_esc(nome_veiculo), negrito))
    if os_.get("ano"):
        veiculo.append(Paragraph(f"Ano: {_esc(os_['ano'])}", normal))
    if os_.get("placa"):
        veiculo.append(Paragraph(f"Placa: {_esc(os_['placa'])}", normal))
    if len(veiculo) == 1:
        veiculo.append(Paragraph("Não informado", normal))
    bloco = Table([[cliente, veiculo]], colWidths=[largura / 2] * 2)
    bloco.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 6)]))
    partes.append(bloco)

    for rotulo, chave in (("Defeito relatado", "defeito"), ("Serviço realizado", "servico")):
        if os_.get(chave):
            partes += [Paragraph(rotulo, secao), Paragraph(_esc(os_[chave]), normal)]

    # pecas
    linhas = [[Paragraph("Peça", negrito), Paragraph("Qtd.", direita_b),
               Paragraph("Preço", direita_b), Paragraph("Total", direita_b)]]
    for item in os_["itens"]:
        linhas.append([Paragraph(_esc(item["descricao"]), normal),
                       Paragraph(f"{item['quantidade']:g}".replace(".", ","), direita),
                       Paragraph(_r(item["preco"]), direita),
                       Paragraph(_r(item["quantidade"] * item["preco"]), direita)])
    if os_.get("mao_de_obra"):
        linhas.append([Paragraph("Mão de obra", normal), "", "", Paragraph(_r(os_["mao_de_obra"]), direita)])
    if len(linhas) > 1:
        tabela = Table(linhas, colWidths=[largura * 0.46, largura * 0.12, largura * 0.21, largura * 0.21],
                       repeatRows=1)
        tabela.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), CINZA),
            ("LINEBELOW", (0, 0), (-1, -1), 0.6, colors.black),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
        partes += [Paragraph("Peças e serviços", secao), tabela]

    # total e pagamento
    total = Table([[Paragraph("TOTAL", ParagraphStyle("tt", parent=titulo, fontSize=18)),
                    Paragraph(_r(os_["total"]), ParagraphStyle("tv", parent=titulo, alignment=TA_RIGHT))]],
                  colWidths=[largura / 2] * 2)
    total.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 2, colors.black),
                               ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 10)]))
    partes += [Spacer(1, 12), total, Spacer(1, 8)]

    if os_["status"] == "PAGO":
        texto = f"<b>Situação: PAGO</b>"
        if os_.get("forma_pagamento"):
            texto += f" em {_esc(os_['forma_pagamento'])}"
        if os_.get("data_pagamento"):
            texto += f", dia {_data(os_['data_pagamento'])}"
    elif os_["status"] == "PARCIAL":
        texto = (f"<b>Situação: PAGO EM PARTE</b><br/>Pago: {_r(os_['valor_pago'])}"
                 f"<br/><b>Falta pagar: {_r(os_['falta'])}</b>")
    else:
        texto = f"<b>Situação: A PAGAR</b>"
    partes.append(Paragraph(texto, normal))

    # fotos: duas por linha, cada uma com legenda e o problema anotado
    if fotos:
        partes += [PageBreak(), Paragraph("Fotos", ParagraphStyle("f", parent=titulo, fontSize=18)), Spacer(1, 6)]
        largura_cel = largura / 2 - 8
        celulas = []
        for foto in fotos:
            imagem = _imagem(foto["arquivo"], largura_cel, 95 * mm)
            if not imagem:
                continue
            bloco_foto = [Paragraph(_esc(foto["legenda"]), negrito), Spacer(1, 3), imagem]
            if foto.get("problema"):
                bloco_foto += [Spacer(1, 3), Paragraph(_esc(foto["problema"]), normal)]
            celulas.append(bloco_foto)
        linhas_fotos = [celulas[i:i + 2] + [""] * (2 - len(celulas[i:i + 2])) for i in range(0, len(celulas), 2)]
        for linha in linhas_fotos:
            grade = Table([linha], colWidths=[largura / 2] * 2)
            grade.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                       ("BOTTOMPADDING", (0, 0), (-1, -1), 12)]))
            partes.append(KeepTogether(grade))

    doc.build(partes)
    buffer.seek(0)
    return buffer
