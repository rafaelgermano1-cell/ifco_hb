#!/usr/bin/env python3
"""Compara modelos avançados de previsão de consumo diário de caixas."""

from __future__ import annotations

import json
import os
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from statsmodels.tsa.arima.model import ARIMA
from statsmodels.tsa.holtwinters import ExponentialSmoothing
from statsmodels.tsa.statespace.sarimax import SARIMAX

warnings.filterwarnings("ignore")

PASTA = Path(os.environ.get("IFCO_HB_PASTA_SAIDA", "dados_processados"))
TREINO = PASTA / "dados_treino_previsao.csv"
TESTE = PASTA / "dados_teste_previsao.csv"
SAIDA_PREVISOES = PASTA / "previsoes_teste_modelos_avancados.csv"
SAIDA_METRICAS = PASTA / "comparacao_modelos_avancados.csv"
SAIDA_RELATORIO = PASTA / "relatorio_modelos_avancados.json"
DIMENSOES = ["origem", "familia_caixa", "modelo_caixa_previsao"]
MODELOS = ["mediana_sazonal", "media_movel_3", "holt_winters", "arima_111", "sarima_111_100_6"]


def limite_inferior(valor: float) -> float:
    return max(0.0, float(valor)) if np.isfinite(valor) else 0.0


def mediana_sazonal(historico: pd.DataFrame, dia: int, familia: str, modelo: str, completo: pd.DataFrame) -> float:
    valores = historico[historico.numero_dia_semana == dia].quantidade_caixas.to_numpy(float)
    if not len(valores):
        valores = completo[(completo.familia_caixa == familia) & (completo.modelo_caixa_previsao == modelo) & (completo.numero_dia_semana == dia)].quantidade_caixas.to_numpy(float)
    return float(np.median(valores)) if len(valores) else 0.0


def previsoes_avancadas(historico: pd.DataFrame, historico_total: pd.DataFrame, dia: int, familia: str, modelo: str) -> tuple[dict[str, float], dict[str, str]]:
    y = historico.quantidade_caixas.to_numpy(float)
    base = mediana_sazonal(historico, dia, familia, modelo, historico_total)
    resultados = {"mediana_sazonal": base, "media_movel_3": float(np.mean(y[-3:])) if len(y) else base}
    origem = {"mediana_sazonal": "ajustado", "media_movel_3": "ajustado" if len(y) >= 3 else "fallback_mediana"}
    # Modelos estatísticos são ajustados apenas quando há sinal e histórico suficiente.
    elegivel = len(y) >= 30 and np.count_nonzero(y) >= 8 and np.std(y) > 0
    if not elegivel:
        for nome in ["holt_winters", "arima_111", "sarima_111_100_6"]:
            resultados[nome] = base; origem[nome] = "fallback_mediana"
        return resultados, origem
    try:
        ajuste = ExponentialSmoothing(y, trend="add", seasonal="add", seasonal_periods=6, initialization_method="estimated").fit(optimized=True)
        resultados["holt_winters"] = limite_inferior(ajuste.forecast(1)[0]); origem["holt_winters"] = "ajustado"
    except Exception:
        resultados["holt_winters"] = base; origem["holt_winters"] = "fallback_mediana"
    try:
        ajuste = ARIMA(y, order=(1, 1, 1), trend="t").fit()
        resultados["arima_111"] = limite_inferior(ajuste.forecast(1)[0]); origem["arima_111"] = "ajustado"
    except Exception:
        resultados["arima_111"] = base; origem["arima_111"] = "fallback_mediana"
    try:
        ajuste = SARIMAX(y, order=(1, 1, 1), seasonal_order=(1, 0, 0, 6), trend="c", enforce_stationarity=False, enforce_invertibility=False).fit(disp=False, maxiter=60)
        resultados["sarima_111_100_6"] = limite_inferior(ajuste.forecast(1)[0]); origem["sarima_111_100_6"] = "ajustado"
    except Exception:
        resultados["sarima_111_100_6"] = base; origem["sarima_111_100_6"] = "fallback_mediana"
    return resultados, origem


def calcular_metricas(teste: pd.DataFrame, coluna: str, coluna_origem: str) -> dict[str, float]:
    erro = teste[coluna] - teste.real
    absoluto = erro.abs()
    positivos = teste.real > 0
    return {
        "MAE": float(absoluto.mean()), "RMSE": float(np.sqrt(np.mean(erro ** 2))),
        "WAPE_pct": float(100 * absoluto.sum() / max(teste.real.sum(), 1e-9)), "viés": float(erro.sum()),
        "MAPE_pct_positivos": float(100 * (absoluto[positivos] / teste.loc[positivos, "real"]).mean()) if positivos.any() else np.nan,
        "observacoes": int(len(teste)), "cobertura_ajuste_pct": float(100 * (teste[coluna_origem] == "ajustado").mean()),
    }


def main() -> None:
    treino = pd.read_csv(TREINO, parse_dates=["data"])
    teste_base = pd.read_csv(TESTE, parse_dates=["data"])
    completo = pd.concat([treino, teste_base], ignore_index=True).sort_values("data")
    datas_teste = sorted(teste_base.data.unique())
    linhas = []
    for chaves, serie in completo.groupby(DIMENSOES):
        serie = serie.sort_values("data")
        origem, familia, modelo = chaves
        for data in datas_teste:
            atual = serie[serie.data == data]
            historico = serie[serie.data < data]
            historico_total = completo[completo.data < data]
            valores, metodos = previsoes_avancadas(historico, historico_total, int(pd.Timestamp(data).dayofweek), familia, modelo)
            linhas.append({"data": pd.Timestamp(data).strftime("%Y-%m-%d"), "origem": origem, "familia_caixa": familia, "modelo_caixa": modelo, "dia_semana": atual.iloc[0].dia_semana, "real": float(atual.iloc[0].quantidade_caixas), **valores, **{f"metodo_{nome}": metodos[nome] for nome in MODELOS}})
    previsoes = pd.DataFrame(linhas)
    previsoes.to_csv(SAIDA_PREVISOES, index=False, encoding="utf-8")
    metricas = []
    for nome in MODELOS:
        metricas.append({"escopo": "geral", "modelo_previsao": nome, **calcular_metricas(previsoes, nome, f"metodo_{nome}")})
        for (familia, modelo), grupo in previsoes.groupby(["familia_caixa", "modelo_caixa"]):
            metricas.append({"escopo": f"{familia} {modelo}", "modelo_previsao": nome, **calcular_metricas(grupo, nome, f"metodo_{nome}")})
    tabela = pd.DataFrame(metricas).sort_values(["escopo", "WAPE_pct", "MAE"])
    tabela.to_csv(SAIDA_METRICAS, index=False, encoding="utf-8")
    geral = tabela[tabela.escopo == "geral"].sort_values(["WAPE_pct", "MAE"])
    relatorio = {"treino": [treino.data.min().strftime("%Y-%m-%d"), treino.data.max().strftime("%Y-%m-%d")], "teste": [teste_base.data.min().strftime("%Y-%m-%d"), teste_base.data.max().strftime("%Y-%m-%d")], "metrica_selecao": "WAPE_pct", "ranking_geral": geral.to_dict(orient="records"), "melhor_modelo": geral.iloc[0].modelo_previsao}
    SAIDA_RELATORIO.write_text(json.dumps(relatorio, ensure_ascii=False, indent=2), encoding="utf-8")
    print(geral[["modelo_previsao", "WAPE_pct", "MAE", "RMSE", "viés", "cobertura_ajuste_pct"]].to_string(index=False))


if __name__ == "__main__":
    main()
