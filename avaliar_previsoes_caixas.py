#!/usr/bin/env python3
"""Audita a base e compara previsões diárias de caixas IFCO/HB.

O teste é temporal: as últimas 18 datas operacionais (três semanas de
segunda a sábado) ficam fora do treino. Cada previsão usa apenas dados
anteriores à data prevista.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import numpy as np
import pandas as pd

PASTA = Path(os.environ.get("IFCO_HB_PASTA_SAIDA", "dados_processados"))
ENTRADA = PASTA / "uso_caixas_diario_previsao.csv"
ARQ_AUDITORIA = PASTA / "auditoria_base_previsao.json"
ARQ_TESTE = PASTA / "previsoes_teste_modelos.csv"
ARQ_METRICAS = PASTA / "comparacao_modelos_previsao.csv"
ARQ_TREINO = PASTA / "dados_treino_previsao.csv"
ARQ_BASE_TESTE = PASTA / "dados_teste_previsao.csv"
DIAS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado"]
DIMENSOES = ["origem", "familia_caixa", "modelo_caixa_previsao"]
MODELOS = ["ultima_semana", "media_dia_semana", "mediana_dia_semana", "media_exponencial", "tendencia_dia_semana"]
DIAS_TESTE = 18


def auditar_base(dados: pd.DataFrame) -> dict[str, object]:
    obrigatorias = {"data", "dia_semana", "numero_dia_semana", "origem", "familia_caixa", "modelo_caixa_previsao", "quantidade_caixas"}
    faltantes = sorted(obrigatorias - set(dados.columns))
    texto = dados.select_dtypes(include="object").fillna("").astype(str)
    caracteres_problematicos = int(texto.apply(lambda coluna: coluna.str.contains(r"[\x00-\x08\x0B\x0C\x0E-\x1F�]", regex=True).sum()).sum())
    validos_modelo = {"6416", "6420", "6424", "618", "623", "NAO_INFORMADO", "NAO INFORMADO", "NÃO INFORMADO"}
    dados["modelo_caixa_previsao"] = dados["modelo_caixa_previsao"].map(lambda valor: "NAO_INFORMADO" if str(valor).strip().upper() in {"NAO_INFORMADO", "NAO INFORMADO", "NÃO INFORMADO"} else str(valor).strip())
    esperado_dia = dados["data"].dt.dayofweek.map(dict(enumerate(DIAS)))
    return {
        "linhas": int(len(dados)), "colunas": list(dados.columns), "colunas_obrigatorias_faltantes": faltantes,
        "datas_invalidas": int(dados["data"].isna().sum()), "origens_vazias": int(dados["origem"].isna().sum() + (dados["origem"].fillna("").str.strip() == "").sum()),
        "quantidades_invalidas": int((~np.isfinite(dados["quantidade_caixas"])).sum()), "quantidades_negativas": int((dados["quantidade_caixas"] < 0).sum()),
        "domingos": int((dados["numero_dia_semana"] == 6).sum()), "dias_inconsistentes": int((dados["dia_semana"] != esperado_dia).sum()),
        "familias_invalidas": sorted(set(dados["familia_caixa"]) - {"IFCO", "HB"}),
        "modelos_invalidos": sorted(set(dados["modelo_caixa_previsao"].astype(str)) - validos_modelo),
        "caracteres_problematicos": caracteres_problematicos,
        "datas": [dados["data"].min().strftime("%Y-%m-%d"), dados["data"].max().strftime("%Y-%m-%d")],
    }


def completar_series(dados: pd.DataFrame) -> pd.DataFrame:
    agregados = dados.groupby(["data", *DIMENSOES], as_index=False)["quantidade_caixas"].sum()
    datas = pd.date_range(agregados["data"].min(), agregados["data"].max(), freq="D")
    datas = datas[datas.dayofweek <= 5]
    partes = []
    for chaves, grupo in agregados.groupby(DIMENSOES, dropna=False):
        base = pd.DataFrame({"data": datas})
        for coluna, valor in zip(DIMENSOES, chaves):
            base[coluna] = valor
        partes.append(base.merge(grupo, on=["data", *DIMENSOES], how="left"))
    resultado = pd.concat(partes, ignore_index=True)
    resultado["quantidade_caixas"] = resultado["quantidade_caixas"].fillna(0.0)
    resultado["numero_dia_semana"] = resultado["data"].dt.dayofweek
    resultado["dia_semana"] = resultado["numero_dia_semana"].map(dict(enumerate(DIAS)))
    return resultado


def prever(historico: pd.DataFrame, historico_total: pd.DataFrame, dia: int, familia: str, modelo: str) -> dict[str, float]:
    propria = historico[historico["numero_dia_semana"] == dia]["quantidade_caixas"].to_numpy(dtype=float)
    # Fallback: mesmo tipo de caixa, agregando origens, mas sem olhar o futuro.
    fallback = historico_total[(historico_total["familia_caixa"] == familia) & (historico_total["modelo_caixa_previsao"] == modelo) & (historico_total["numero_dia_semana"] == dia)]["quantidade_caixas"].to_numpy(dtype=float)
    valores = propria if len(propria) else fallback
    if not len(valores):
        return {nome: 0.0 for nome in MODELOS}
    pesos = np.exp(np.linspace(-2.0, 0.0, len(valores)))
    recentes = propria[-min(8, len(propria)):]
    if len(recentes) >= 2:
        inclinacao, intercepto = np.polyfit(np.arange(len(recentes)), recentes, 1)
        tendencia = max(0.0, float(intercepto + inclinacao * len(recentes)))
    else:
        tendencia = float(valores[-1])
    return {
        "ultima_semana": float(valores[-1]),
        "media_dia_semana": float(np.mean(valores)),
        "mediana_dia_semana": float(np.median(valores)),
        "media_exponencial": float(np.average(valores, weights=pesos)),
        "tendencia_dia_semana": tendencia,
    }


def metricas(resultado: pd.DataFrame, col_predicao: str) -> dict[str, float]:
    erro = resultado[col_predicao] - resultado["real"]
    absoluto = erro.abs()
    reais_positivos = resultado["real"] > 0
    return {
        "MAE": float(absoluto.mean()), "RMSE": float(np.sqrt(np.mean(np.square(erro)))),
        "WAPE_pct": float(100 * absoluto.sum() / max(resultado["real"].sum(), 1e-9)),
        "viés": float(erro.sum()),
        "MAPE_pct_positivos": float(100 * (absoluto[reais_positivos] / resultado.loc[reais_positivos, "real"]).mean()) if reais_positivos.any() else np.nan,
        "observacoes": int(len(resultado)), "caixas_reais": float(resultado["real"].sum()),
    }


def executar() -> None:
    dados = pd.read_csv(ENTRADA, parse_dates=["data"])
    dados["quantidade_caixas"] = pd.to_numeric(dados["quantidade_caixas"], errors="coerce")
    auditoria = auditar_base(dados)
    problemas = any([auditoria["colunas_obrigatorias_faltantes"], auditoria["datas_invalidas"], auditoria["origens_vazias"], auditoria["quantidades_invalidas"], auditoria["quantidades_negativas"], auditoria["domingos"], auditoria["dias_inconsistentes"], auditoria["familias_invalidas"], auditoria["modelos_invalidos"], auditoria["caracteres_problematicos"]])
    auditoria["apta_para_previsao"] = not problemas
    if problemas:
        ARQ_AUDITORIA.write_text(json.dumps(auditoria, ensure_ascii=False, indent=2), encoding="utf-8")
        raise ValueError("A base não passou na auditoria; consulte auditoria_base_previsao.json")
    completo = completar_series(dados)
    datas_teste = sorted(completo["data"].unique())[-DIAS_TESTE:]
    data_corte = pd.Timestamp(datas_teste[0])
    completo[completo["data"] < data_corte].to_csv(ARQ_TREINO, index=False, encoding="utf-8")
    completo[completo["data"] >= data_corte].to_csv(ARQ_BASE_TESTE, index=False, encoding="utf-8")
    previsoes = []
    for chaves, serie in completo.groupby(DIMENSOES, dropna=False):
        serie = serie.sort_values("data")
        origem, familia, modelo = chaves
        for data_teste in datas_teste:
            atual = serie[serie["data"] == data_teste]
            if atual.empty:
                continue
            historico = completo[completo["data"] < data_teste]
            historico_serie = historico[(historico["origem"] == origem) & (historico["familia_caixa"] == familia) & (historico["modelo_caixa_previsao"] == modelo)]
            valores = prever(historico_serie, historico, int(pd.Timestamp(data_teste).dayofweek), familia, modelo)
            linha = {"data": pd.Timestamp(data_teste).strftime("%Y-%m-%d"), "origem": origem, "familia_caixa": familia, "modelo_caixa": modelo, "dia_semana": DIAS[pd.Timestamp(data_teste).dayofweek], "real": float(atual.iloc[0]["quantidade_caixas"]), **valores}
            previsoes.append(linha)
    teste = pd.DataFrame(previsoes)
    teste.to_csv(ARQ_TESTE, index=False, encoding="utf-8")
    linhas_metricas = []
    for nome in MODELOS:
        linhas_metricas.append({"escopo": "geral", "modelo_previsao": nome, **metricas(teste, nome)})
        for (familia, modelo), grupo in teste.groupby(["familia_caixa", "modelo_caixa"]):
            linhas_metricas.append({"escopo": f"{familia} {modelo}", "modelo_previsao": nome, **metricas(grupo, nome)})
    tabela_metricas = pd.DataFrame(linhas_metricas).sort_values(["escopo", "WAPE_pct", "MAE"])
    tabela_metricas.to_csv(ARQ_METRICAS, index=False, encoding="utf-8")
    geral = tabela_metricas[tabela_metricas["escopo"] == "geral"].sort_values(["WAPE_pct", "MAE"]).iloc[0]
    auditoria.update({"periodo_treino": [completo["data"].min().strftime("%Y-%m-%d"), (data_corte - pd.Timedelta(days=1)).strftime("%Y-%m-%d")], "periodo_teste": [data_corte.strftime("%Y-%m-%d"), pd.Timestamp(datas_teste[-1]).strftime("%Y-%m-%d")], "datas_teste": len(datas_teste), "melhor_modelo_global": geral["modelo_previsao"], "metrica_selecao": "WAPE_pct", "WAPE_pct_melhor_modelo": float(geral["WAPE_pct"])})
    ARQ_AUDITORIA.write_text(json.dumps(auditoria, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"auditoria": auditoria, "metricas_gerais": tabela_metricas[tabela_metricas["escopo"] == "geral"].to_dict(orient="records")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    executar()
