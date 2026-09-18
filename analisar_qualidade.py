"""Gera auditoria de qualidade para um CSV bruto e a base normalizada."""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd


PASTA = Path(os.environ.get("IFCO_HB_PASTA_SAIDA", "dados_processados"))
BRUTO = PASTA / "dados_processados.csv"
NORMALIZADO = PASTA / "uso_caixas_normalizado.csv"
SAIDA = PASTA / "analise_qualidade.json"


def analisar() -> dict[str, object]:
    bruto_linhas = 0
    ausentes: dict[str, int] = {}
    datas_invalidas = 0
    quantidades_invalidas = 0
    quantidades_negativas = 0
    datas_min = None
    datas_max = None
    for bloco in pd.read_csv(BRUTO, usecols=["Data", "Origem", "Desc. Produto", "Desc. Embal.", "Qtd. Caixa"], chunksize=50_000):
        bruto_linhas += len(bloco)
        for coluna in bloco.columns:
            ausentes[coluna] = ausentes.get(coluna, 0) + int(bloco[coluna].isna().sum())
        datas = pd.to_datetime(bloco["Data"], errors="coerce")
        quantidades = pd.to_numeric(bloco["Qtd. Caixa"], errors="coerce")
        datas_invalidas += int(datas.isna().sum())
        quantidades_invalidas += int(quantidades.isna().sum())
        quantidades_negativas += int((quantidades < 0).sum())
        if datas.notna().any():
            atual_min, atual_max = datas.min(), datas.max()
            datas_min = atual_min if datas_min is None or atual_min < datas_min else datas_min
            datas_max = atual_max if datas_max is None or atual_max > datas_max else datas_max

    normalizado = pd.read_csv(NORMALIZADO)
    quantidade = pd.to_numeric(normalizado["quantidade_caixas"], errors="coerce")
    q1, q3 = quantidade.quantile([0.25, 0.75])
    iqr = q3 - q1
    limite_superior = q3 + 1.5 * iqr
    outliers = quantidade > limite_superior
    outlier_quantidade = float(quantidade[outliers].sum())
    total_quantidade = float(quantidade.sum())
    por_familia = normalizado.groupby("familia_caixa")["quantidade_caixas"].agg(["count", "sum"]).to_dict(orient="index")
    por_modelo = normalizado.groupby(["familia_caixa", "modelo_caixa"], dropna=False)["quantidade_caixas"].agg(["count", "sum"]).reset_index()

    return {
        "arquivo": str(BRUTO),
        "linhas_brutas": bruto_linhas,
        "periodo_bruto": [datas_min.strftime("%Y-%m-%d"), datas_max.strftime("%Y-%m-%d")],
        "ausentes_nas_colunas_relevantes": ausentes,
        "datas_invalidas": datas_invalidas,
        "quantidades_invalidas": quantidades_invalidas,
        "quantidades_negativas": quantidades_negativas,
        "linhas_normalizadas": int(len(normalizado)),
        "origens_normalizadas": int(normalizado["origem"].nunique()),
        "familias": sorted(normalizado["familia_caixa"].dropna().unique().tolist()),
        "modelos_por_familia": por_modelo.to_dict(orient="records"),
        "quantidade_total_normalizada": total_quantidade,
        "outliers_iqr_quantidade_por_linha": {
            "q1": float(q1), "q3": float(q3), "limite_superior": float(limite_superior),
            "linhas": int(outliers.sum()), "percentual_linhas": float(100 * outliers.mean()),
            "quantidade": outlier_quantidade,
            "percentual_quantidade": float(100 * outlier_quantidade / max(total_quantidade, 1e-9)),
            "tratamento": "mantidos; representam volumes operacionais e não foram removidos",
        },
        "quantidades_por_familia": por_familia,
        "tratamentos_aplicados": [
            "datas e quantidades inválidas foram excluídas pelo normalizador e contabilizadas no relatório",
            "família/modelo foram padronizados por regras explícitas de identificação",
            "HB sem modelo foi rateado por capacidade quando existia base histórica",
            "outliers de quantidade foram mantidos por serem potencialmente pedidos reais",
        ],
    }


if __name__ == "__main__":
    resultado = analisar()
    SAIDA.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(resultado, ensure_ascii=False, indent=2))
