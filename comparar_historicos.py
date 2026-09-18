"""Compara previsões do histórico recente e completo no mesmo conjunto de casos."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd


RECENTE = Path("dados_processados")
COMPLETO = Path("dados_processados_completo")
SAIDA = COMPLETO / "comparacao_historicos.json"
CHAVES = ["data", "origem", "familia_caixa", "modelo_caixa"]


def metricas(dados: pd.DataFrame, coluna: str) -> dict[str, float]:
    erro = dados[coluna] - dados["real"]
    absoluto = erro.abs()
    return {
        "MAE": float(absoluto.mean()),
        "RMSE": float(np.sqrt(np.mean(erro**2))),
        "WAPE_pct": float(100 * absoluto.sum() / max(dados["real"].sum(), 1e-9)),
        "vies": float(erro.sum()),
        "observacoes": int(len(dados)),
        "caixas_reais": float(dados["real"].sum()),
    }


def comparar(nome: str, coluna_modelo: str) -> dict[str, object]:
    tipos = {chave: str for chave in CHAVES}
    recente = pd.read_csv(RECENTE / nome, usecols=CHAVES + ["real", coluna_modelo], dtype=tipos)
    completo = pd.read_csv(COMPLETO / nome, usecols=CHAVES + ["real", coluna_modelo], dtype=tipos)
    recente = recente.rename(columns={coluna_modelo: "previsao_recente", "real": "real_recente"})
    completo = completo.rename(columns={coluna_modelo: "previsao_completo", "real": "real_completo"})
    unido = completo.merge(recente, on=CHAVES, how="inner")
    if unido.empty:
        raise ValueError(f"Não há casos comuns para comparar em {nome}.")
    base_recente = unido.rename(columns={"previsao_recente": "previsao", "real_recente": "real"})
    base_completo = unido.rename(columns={"previsao_completo": "previsao", "real_completo": "real"})
    return {
        "arquivo": nome,
        "casos_comuns": int(len(unido)),
        "reais_divergentes_por_rateio": int((~np.isclose(unido.real_recente, unido.real_completo)).sum()),
        "diferenca_absoluta_caixas_reais": float((unido.real_completo - unido.real_recente).abs().sum()),
        "recente": metricas(base_recente, "previsao"),
        "completo": metricas(base_completo, "previsao"),
        "diferenca_WAPE_pontos_percentuais": metricas(base_completo, "previsao")["WAPE_pct"] - metricas(base_recente, "previsao")["WAPE_pct"],
    }


def main() -> None:
    resultado = {
        "protocolo": "comparação somente nos mesmos casos (data, origem, família e modelo)",
        "modelos_classicos": [],
        "modelos_avancados": [],
    }
    for modelo in ["ultima_semana", "media_dia_semana", "mediana_dia_semana", "media_exponencial", "tendencia_dia_semana"]:
        item = comparar("previsoes_teste_modelos.csv", modelo)
        item["modelo"] = modelo
        resultado["modelos_classicos"].append(item)
    for modelo in ["mediana_sazonal", "media_movel_3", "holt_winters", "arima_111", "sarima_111_100_6"]:
        item = comparar("previsoes_teste_modelos_avancados.csv", modelo)
        item["modelo"] = modelo
        resultado["modelos_avancados"].append(item)
    SAIDA.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(resultado, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
