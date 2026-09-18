#!/usr/bin/env python3
"""Prepara a base diária de previsão de caixas, de segunda a sábado.

Registros com HB sem modelo explícito são distribuídos pelos modelos HB usando
a proporção histórica de quantidade para a mesma capacidade (C/ xxUN ou KG).
O dado bruto não é alterado: ``metodo_modelo`` registra cada rateio.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pandas as pd

PASTA = Path(os.environ.get("IFCO_HB_PASTA_SAIDA", "dados_processados"))
ENTRADA = PASTA / "uso_caixas_normalizado.csv"
SAIDA = PASTA / "uso_caixas_diario_previsao.csv"
RELATORIO = PASTA / "relatorio_tratamento_modelos_hb.json"
MODELO_NAO_INFORMADO = "NAO_INFORMADO"
MODELOS_NAO_INFORMADOS = {"NAO_INFORMADO", "NAO INFORMADO", "NÃO INFORMADO"}


def normalizar_modelo(valor: object) -> str:
    if pd.isna(valor):
        return MODELO_NAO_INFORMADO
    texto = str(valor).strip().upper()
    return MODELO_NAO_INFORMADO if texto in MODELOS_NAO_INFORMADOS else texto


def capacidade(descricao: object) -> str | None:
    resultado = re.search(r"C/\s*(\d+)\s*(UN|KG)", str(descricao).upper())
    return f"{resultado.group(1)}{resultado.group(2)}" if resultado else None


def preparar_base() -> dict[str, object]:
    dados = pd.read_csv(ENTRADA, parse_dates=["data"])
    dados["modelo_caixa"] = dados["modelo_caixa"].map(normalizar_modelo)
    dados["quantidade_caixas"] = pd.to_numeric(dados["quantidade_caixas"], errors="raise")
    dados["capacidade_embalagem"] = dados["desc_embalagem_original"].map(capacidade)
    # Somente segunda a sábado participam do modelo de previsão.
    dados = dados[dados["numero_dia_semana"] <= 5].copy()
    conhecidos = dados[(dados["familia_caixa"] == "HB") & (dados["modelo_caixa"] != MODELO_NAO_INFORMADO) & dados["capacidade_embalagem"].notna()]
    pesos = (conhecidos.groupby(["capacidade_embalagem", "modelo_caixa"])["quantidade_caixas"].sum()
             .groupby(level=0).transform(lambda serie: serie / serie.sum()))
    pesos = pesos.to_dict()
    linhas: list[dict[str, object]] = []
    quantidade_rateada = 0.0
    registros_rateados = 0
    for registro in dados.itertuples(index=False):
        registro_dict = registro._asdict()
        if registro_dict["familia_caixa"] != "HB" or registro_dict["modelo_caixa"] != MODELO_NAO_INFORMADO:
            linhas.append({**registro_dict, "modelo_caixa_previsao": registro_dict["modelo_caixa"], "metodo_modelo": "explícito"})
            continue
        opcoes = [(modelo, peso) for (cap, modelo), peso in pesos.items() if cap == registro_dict["capacidade_embalagem"]]
        if not opcoes:
            linhas.append({**registro_dict, "modelo_caixa_previsao": MODELO_NAO_INFORMADO, "metodo_modelo": "sem_base_para_rateio"})
            continue
        for modelo, peso in opcoes:
            linhas.append({**registro_dict, "modelo_caixa_previsao": modelo, "metodo_modelo": "rateio_por_capacidade", "quantidade_caixas": registro_dict["quantidade_caixas"] * peso})
        quantidade_rateada += registro_dict["quantidade_caixas"]
        registros_rateados += 1
    resultado = pd.DataFrame(linhas)
    diario = (resultado.groupby(["data", "dia_semana", "numero_dia_semana", "origem", "familia_caixa", "modelo_caixa_previsao", "metodo_modelo"], as_index=False)
              .agg(quantidade_caixas=("quantidade_caixas", "sum"), registros_origem=("quantidade_caixas", "size"))
              .sort_values(["data", "origem", "familia_caixa", "modelo_caixa_previsao"]))
    diario["data"] = diario["data"].dt.strftime("%Y-%m-%d")
    diario.to_csv(SAIDA, index=False, encoding="utf-8")
    total_entrada = dados["quantidade_caixas"].sum()
    total_saida = diario["quantidade_caixas"].sum()
    return {
        "regra": "HB sem modelo é rateado pela participação histórica de cada modelo na mesma capacidade de embalagem.",
        "periodo": [dados["data"].min().strftime("%Y-%m-%d"), dados["data"].max().strftime("%Y-%m-%d")],
        "dias_incluidos": ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado"],
        "registros_hb_rateados": registros_rateados,
        "quantidade_hb_rateada": float(quantidade_rateada),
        "total_caixas_antes": float(total_entrada),
        "total_caixas_depois": float(total_saida),
        "totais_preservados": bool(abs(total_entrada - total_saida) < 1e-8),
        "pesos_historicos_por_capacidade": {f"{cap}|{modelo}": peso for (cap, modelo), peso in pesos.items()},
    }


if __name__ == "__main__":
    resultado = preparar_base()
    RELATORIO.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(resultado, ensure_ascii=False, indent=2))
