#!/usr/bin/env python3
"""Normaliza o uso de caixas IFCO/HB para a etapa de previsão diária.

Lê o CSV de vendas por blocos. A identificação dá prioridade a ``Desc. Embal.``
e usa ``Desc. Produto`` quando a embalagem não informa IFCO/HB. Nenhum registro
é descartado silenciosamente: os não classificados ficam contabilizados no
relatório de auditoria.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
from collections import Counter
from pathlib import Path

import pandas as pd

PASTA_SAIDA = Path(os.environ.get("IFCO_HB_PASTA_SAIDA", "dados_processados"))
ARQUIVO_ENTRADA = PASTA_SAIDA / "dados_processados.csv"
ARQUIVO_NORMALIZADO = PASTA_SAIDA / "uso_caixas_normalizado.csv"
ARQUIVO_DIARIO = PASTA_SAIDA / "uso_caixas_diario.csv"
ARQUIVO_RELATORIO = PASTA_SAIDA / "relatorio_normalizacao_caixas.json"
ARQUIVO_BANCO = PASTA_SAIDA / "_agregacao_caixas.sqlite"
TAMANHO_BLOCO = 20_000
MODELO_NAO_INFORMADO = "NAO_INFORMADO"
MODELOS_NAO_INFORMADOS = {"NAO_INFORMADO", "NAO INFORMADO", "NÃO INFORMADO"}


def normalizar_modelo(valor: object) -> str:
    """Padroniza as marcações de modelo ausente em um único valor canônico."""
    if pd.isna(valor):
        return MODELO_NAO_INFORMADO
    texto = str(valor).strip().upper()
    if texto in MODELOS_NAO_INFORMADOS:
        return MODELO_NAO_INFORMADO
    return texto


DIAS_SEMANA = {
    0: "segunda-feira", 1: "terça-feira", 2: "quarta-feira",
    3: "quinta-feira", 4: "sexta-feira", 5: "sábado", 6: "domingo",
}


def normalizar_texto(valor: object) -> str:
    """Padroniza somente para classificação; o texto original também é salvo."""
    if pd.isna(valor):
        return ""
    return re.sub(r"\s+", " ", str(valor).upper()).strip()


def identificar_caixa(descricao_embalagem: object, descricao_produto: object) -> tuple[str | None, str | None, str | None, str]:
    """Retorna família, modelo, campo usado e texto de evidência.

    Modelos conhecidos: IFCO 6416/6420/6424 e HB 618/623. Se uma descrição
    aponta IFCO/HB sem número de modelo, a família é mantida e o modelo recebe
    ``NAO_INFORMADO`` para revisão, nunca uma suposição.
    """
    embalagem = normalizar_texto(descricao_embalagem)
    produto = normalizar_texto(descricao_produto)
    for campo, texto in (("Desc. Embal.", embalagem), ("Desc. Produto", produto)):
        marca = "IFCO" if "IFCO" in texto else "HB" if re.search(r"\bHB\b", texto) else None
        if not marca:
            continue
        if marca == "IFCO":
            encontrado = re.search(r"\b(64(?:16|20|24))\b", texto)
        else:
            encontrado = re.search(r"\b(618|623)\b", texto)
        return marca, encontrado.group(1) if encontrado else MODELO_NAO_INFORMADO, campo, texto
    return None, None, None, ""


def preparar_banco() -> sqlite3.Connection:
    if ARQUIVO_BANCO.exists():
        ARQUIVO_BANCO.unlink()
    conexao = sqlite3.connect(ARQUIVO_BANCO)
    conexao.execute("""CREATE TABLE uso_diario (
        data TEXT NOT NULL, origem TEXT NOT NULL, familia TEXT NOT NULL,
        modelo TEXT NOT NULL, dia_semana TEXT NOT NULL, numero_dia_semana INTEGER NOT NULL,
        quantidade_caixas REAL NOT NULL, registros INTEGER NOT NULL,
        PRIMARY KEY (data, origem, familia, modelo)
    )""")
    return conexao


def normalizar_dados() -> dict[str, object]:
    if not ARQUIVO_ENTRADA.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {ARQUIVO_ENTRADA}")
    colunas = ["Data", "Origem", "Desc. Produto", "Desc. Embal.", "Qtd. Caixa"]
    estatisticas: Counter[str] = Counter()
    pendencias: Counter[str] = Counter()
    conexao = preparar_banco()
    with ARQUIVO_NORMALIZADO.open("w", encoding="utf-8", newline="") as destino:
        escritor = csv.DictWriter(destino, fieldnames=[
            "data", "dia_semana", "numero_dia_semana", "origem", "familia_caixa",
            "modelo_caixa", "quantidade_caixas", "fonte_classificacao",
            "desc_embalagem_original", "desc_produto_original",
        ])
        escritor.writeheader()
        for bloco in pd.read_csv(ARQUIVO_ENTRADA, usecols=colunas, chunksize=TAMANHO_BLOCO):
            estatisticas["linhas_lidas"] += len(bloco)
            bloco["Data"] = pd.to_datetime(bloco["Data"], errors="coerce")
            bloco["Qtd. Caixa"] = pd.to_numeric(bloco["Qtd. Caixa"], errors="coerce")
            for registro in bloco.itertuples(index=False):
                data, origem, produto, embalagem, quantidade = registro
                familia, modelo, fonte, evidencia = identificar_caixa(embalagem, produto)
                if familia is None:
                    estatisticas["linhas_sem_ifco_hb"] += 1
                    continue
                if pd.isna(data) or pd.isna(quantidade):
                    estatisticas["linhas_ifco_hb_invalidas"] += 1
                    continue
                if quantidade < 0:
                    estatisticas["linhas_ifco_hb_negativas"] += 1
                modelo = normalizar_modelo(modelo)
                if modelo == MODELO_NAO_INFORMADO:
                    pendencias[f"{familia} | {evidencia}"] += 1
                numero_dia = int(data.weekday())
                linha = {
                    "data": data.strftime("%Y-%m-%d"), "dia_semana": DIAS_SEMANA[numero_dia],
                    "numero_dia_semana": numero_dia, "origem": str(origem).strip(),
                    "familia_caixa": familia, "modelo_caixa": modelo,
                    "quantidade_caixas": format(float(quantidade), ".15g"),
                    "fonte_classificacao": fonte, "desc_embalagem_original": "" if pd.isna(embalagem) else str(embalagem),
                    "desc_produto_original": "" if pd.isna(produto) else str(produto),
                }
                escritor.writerow(linha)
                conexao.execute("""INSERT INTO uso_diario VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(data, origem, familia, modelo) DO UPDATE SET
                    quantidade_caixas = quantidade_caixas + excluded.quantidade_caixas,
                    registros = registros + 1""", (
                    linha["data"], linha["origem"], familia, modelo, linha["dia_semana"], numero_dia, float(quantidade),
                ))
                estatisticas["linhas_normalizadas"] += 1
                estatisticas[f"familia_{familia}"] += 1
                estatisticas[f"fonte_{fonte}"] += 1
            conexao.commit()
            print(f"Registros analisados: {estatisticas['linhas_lidas']:,}")
    with ARQUIVO_DIARIO.open("w", encoding="utf-8", newline="") as destino:
        escritor = csv.writer(destino)
        escritor.writerow(["data", "dia_semana", "numero_dia_semana", "origem", "familia_caixa", "modelo_caixa", "quantidade_caixas", "registros_origem"])
        for linha in conexao.execute("SELECT data, dia_semana, numero_dia_semana, origem, familia, modelo, quantidade_caixas, registros FROM uso_diario ORDER BY data, origem, familia, modelo"):
            escritor.writerow(linha)
    conexao.close()
    ARQUIVO_BANCO.unlink(missing_ok=True)
    return {"estatisticas": dict(estatisticas), "pendencias_modelo": pendencias.most_common(50)}


def main() -> None:
    print("[1/3] Lendo e classificando IFCO/HB...")
    resultado = normalizar_dados()
    relatorio = {
        "entrada": str(ARQUIVO_ENTRADA), "regra": "Desc. Embal. tem prioridade; Desc. Produto é usado como alternativa.",
        "modelos_esperados": {"IFCO": ["6416", "6420", "6424"], "HB": ["618", "623"]}, **resultado,
    }
    ARQUIVO_RELATORIO.write_text(json.dumps(relatorio, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[2/3] Agregação diária por origem, família e modelo concluída.")
    print("[3/3] Arquivos normalizados gravados em dados_processados.")
    print(json.dumps(resultado["estatisticas"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
