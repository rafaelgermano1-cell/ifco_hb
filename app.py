"""Dashboard local de consumo de caixas IFCO/HB.

Execute com: streamlit run app.py
"""

from pathlib import Path
import re
import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
import streamlit as st


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "dados_processados_completo"
NORMALIZED = OUTPUT_DIR / "uso_caixas_normalizado.csv"
RAW = OUTPUT_DIR / "dados_processados.csv"
REQUIRED_MODELS = ["618", "623", "6416", "6420", "6424"]
MODEL_LABELS = {
    "618": "HB 618",
    "623": "HB 623",
    "6416": "IFCO 6416",
    "6420": "IFCO 6420",
    "6424": "IFCO 6424",
}
LABEL_TO_MODEL = {label: model for model, label in MODEL_LABELS.items()}
BUFFER_MINIMO = 0.30
JANELA_BUFFER_DIAS = 56
ESTOQUE_MINIMO_6420 = 1500
DIAS_SEMANA = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
MESES_ABREVIADOS = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
ARAGUARI_PACKINGS = {"PH ARAGUARI - GRANEL", "PH ARAGUARI - EMBALADOS"}
PACKING_CIDADE = {
    "PH ARAGUARI - GRANEL": "PH ARAGUARI",
    "PH ARAGUARI - EMBALADOS": "PH ARAGUARI",
    "PH ANAPOLIS - EMBALADO": "PH ANAPOLIS",
    "PH ANAPOLIS GRANEL": "PH ANAPOLIS",
    "PH CEAGESP - EMBALADOS": "PH CEAGESP",
    "PH CEAGESP - GRANEL": "PH CEAGESP",
    "PH LEBON REGIS - EMBALADO": "PH LEBON REGIS",
    "PH LEBON REGIS - GRANEL": "PH LEBON REGIS",
    "PH UBAJARA-CE": "PH UBAJARA",
    "PH UBAJARA-CE (PJ)": "PH UBAJARA",
    "BOX CEASA GOIANIA": "PH GOIANIA",
    "CD GOIANIA": "PH GOIANIA",
    "INATIVO - PH GOIANIA - EMBALADOS": "PH GOIANIA",
    "BOX CEASA DF": "PH CEASA DF",
    "INATIVO - PH CEASA DF - GRANEL": "PH CEASA DF",
}


def nome_packing(valor):
    return PACKING_CIDADE.get(valor, valor)


def formatar_data(valor):
    data = pd.Timestamp(valor)
    return f"{data.day:02d}/{MESES_ABREVIADOS[data.month - 1]}/{data.year % 100:02d} - {DIAS_SEMANA[data.weekday()]}"


def formatar_numero(valor):
    return f"{math.ceil(float(valor)):,}".replace(",", ".")


def destacar_negativo(valor):
    return "color: #b42318; background-color: #fde8e7;" if float(valor) < 0 else ""


def corrigir_inconsistencias_quantidade(raw):
    """Corrige o registro conhecido com escala decimal perdida em Qtd. Caixa."""
    mascara = (
        (raw["Data"].astype(str).str.startswith("2026-09-10"))
        & (raw["Origem"].astype(str).str.strip() == "TREB. MINAS")
        & (raw["PACKING"].astype(str).str.strip() == "PH ARAGUARI - EMBALADOS")
        & (raw["Desc. Embal."].fillna("").astype(str).str.strip() == "HB ALTA 623 C/ 16UN")
        & (pd.to_numeric(raw["Qtd. Caixa"], errors="coerce") == 76875)
    )
    raw.loc[mascara, "Qtd. Caixa"] = 76.875
    return raw


def calcular_coletas(forecast, selected_models, stock):
    """Calcula coletas conforme as janelas operacionais de abastecimento."""
    consumo = forecast.pivot(index="data", columns="modelo", values="previsao")
    consumo = consumo.reindex(columns=selected_models, fill_value=0).fillna(0)
    consumo = consumo.sort_index()
    saldo = {modelo: float(stock[modelo]) for modelo in selected_models}
    datas = list(consumo.index)
    janelas = {
        0: [1, 2],       # coleta de segunda abastece terça e quarta
        2: [3, 4],       # coleta de quarta abastece quinta e sexta
        4: [5, 0],       # coleta de sexta abastece sábado e segunda
    }
    linhas = []
    for data_coleta, consumos in consumo.iterrows():
        coletas = {}
        for modelo in selected_models:
            saldo[modelo] -= float(consumos[modelo])
            if data_coleta.weekday() not in {0, 2, 4}:
                continue
            dias_semana = janelas[data_coleta.weekday()]
            datas_abastecidas = [
                data for data in datas
                if data > data_coleta and data.weekday() in dias_semana
            ]
            consumo_janela = float(consumo.loc[datas_abastecidas, modelo].sum()) if datas_abastecidas else 0.0
            estoque_alvo = math.ceil(consumo_janela)
            if modelo == "6420":
                estoque_alvo = max(estoque_alvo, ESTOQUE_MINIMO_6420)
            quantidade = math.ceil(max(0.0, estoque_alvo - saldo[modelo]))
            coletas[modelo] = quantidade
            saldo[modelo] += quantidade
        if data_coleta.weekday() in {0, 2, 4}:
            linhas.append({"data": data_coleta, **coletas})
    return pd.DataFrame(linhas)


@st.cache_data
def load_data():
    """Load the row-level normalized data and retain PACKING from the raw export."""
    if RAW.exists():
        available_columns = pd.read_csv(RAW, nrows=0).columns
        required = ["Data", "Origem", "PACKING", "Qtd. Caixa", "Desc. Embal.", "Desc. Produto"]
        if not set(required).issubset(available_columns):
            required = []
        parts = []
        chunks = (
            pd.read_csv(RAW, usecols=required, chunksize=50_000, low_memory=False)
            if required
            else []
        )
        for raw in chunks:
            raw = corrigir_inconsistencias_quantidade(raw)
            combined = raw["Desc. Embal."].fillna("").astype(str) + " " + raw["Desc. Produto"].fillna("").astype(str)
            raw["modelo"] = combined.str.extract(r"(?<!\d)(6416|6420|6424|618|623)(?!\d)", expand=False)
            raw["data"] = pd.to_datetime(raw["Data"], errors="coerce")
            raw["quantidade"] = pd.to_numeric(raw["Qtd. Caixa"], errors="coerce")
            raw["packing"] = raw["PACKING"].fillna("Sem Packing").astype(str).str.strip().map(nome_packing)
            parte = raw[["data", "packing", "modelo", "quantidade"]].dropna(
                subset=["data", "modelo", "quantidade"]
            )
            if not parte.empty:
                parts.append(parte)
        if parts:
            return pd.concat(parts, ignore_index=True)

    if not NORMALIZED.exists():
        raise FileNotFoundError("Nenhum CSV de dados foi encontrado em dados_processados_completo.")
    normalized = pd.read_csv(NORMALIZED)
    normalized["data"] = pd.to_datetime(normalized["data"], errors="coerce")
    normalized["modelo"] = normalized["modelo_caixa"].astype(str)
    normalized["quantidade"] = pd.to_numeric(normalized["quantidade_caixas"], errors="coerce").fillna(0)
    normalized["packing"] = "Packing não disponível no CSV normalizado"
    return normalized[["data", "packing", "modelo", "quantidade"]].dropna(subset=["data", "modelo"])


def operational_days(start, count):
    days = []
    current = start
    while len(days) < count:
        if current.weekday() != 6:  # domingo
            days.append(current)
        current += timedelta(days=1)
    return days


def metodo_previsao_modelo(modelo: str) -> str:
    """Escolhe o método mais estável por tipo de caixa com base no backtest do histórico completo."""
    if modelo in {"623", "6416"}:
        return "exponencial"
    return "mediana"


def model_forecast(history, models, days):
    """Prevê consumo e aplica cobertura adaptativa para reduzir risco de pico."""
    history = history.copy()
    history["data"] = pd.to_datetime(history["data"]).dt.normalize()
    history = history.groupby(["data", "modelo"], as_index=False)["quantidade"].sum()
    rows = []
    for day in days:
        for model in models:
            serie = history[(history["modelo"] == model) & (history["data"].dt.dayofweek == day.weekday())].copy()
            serie = serie.sort_values("data")
            if serie.empty:
                rows.append({"data": day, "modelo": model, "metodo": "sem_historico", "previsao": 0.0})
                continue
            valores = serie["quantidade"].astype(float).to_numpy()
            mediana = float(np.median(valores))
            pesos = np.exp(np.linspace(-2.0, 0.0, len(valores)))
            media_exponencial = float(np.average(valores, weights=pesos)) if len(valores) else 0.0
            metodo = metodo_previsao_modelo(model)
            if metodo == "exponencial":
                value = media_exponencial
            else:
                value = mediana
            if not np.isfinite(value):
                value = 0.0
            value = max(0.0, float(value))
            base_date = history["data"].max()
            janela = history[
                (history["modelo"] == model)
                & (history["data"] > base_date - pd.Timedelta(days=JANELA_BUFFER_DIAS))
                & (history["data"].dt.dayofweek == day.weekday())
            ]["quantidade"].astype(float)
            pico_recente = float(np.percentile(janela, 75)) if not janela.empty else value
            previsao_operacional = max(value * (1 + BUFFER_MINIMO), pico_recente)
            rows.append(
                {
                    "data": day,
                    "modelo": model,
                    "metodo": metodo,
                    "previsao_base": math.ceil(value),
                    "buffer_seguranca": math.ceil(max(0.0, previsao_operacional - value)),
                    "previsao": math.ceil(previsao_operacional),
                }
            )
    return pd.DataFrame(rows)


st.set_page_config(page_title="IFCO/HB — Consumo e estoque", layout="wide")
st.markdown(
    """
    <style>
    :root {
        --trebeschi-verde: #176b45;
        --trebeschi-verde-escuro: #0d4d34;
        --trebeschi-dourado: #d5a928;
        --trebeschi-creme: #f7f4e8;
    }
    .stApp { background-color: #ffffff; }
    [data-testid="stSidebar"] {
        background-color: #f7f8f6;
        border-right: 1px solid #e0e4df;
    }
    h1, h2, h3 { color: var(--trebeschi-verde-escuro); }
    h1 { font-size: 2.25rem; letter-spacing: -0.03em; }
    h2, h3 { letter-spacing: -0.015em; }
    [data-testid="stHeader"] { background-color: #ffffff; }
    .stButton > button, .stDownloadButton > button {
        background-color: var(--trebeschi-verde);
        color: white;
        border: 1px solid var(--trebeschi-verde-escuro);
    }
    </style>
    """,
    unsafe_allow_html=True,
)
logo = ROOT / "assets" / "trebeschi_logo.jpg"
cabecalho_logo, cabecalho_titulo = st.columns([1, 4], vertical_alignment="center")
with cabecalho_logo:
    if logo.exists():
        st.image(str(logo), width=190)
with cabecalho_titulo:
    st.title("Painel de consumo de caixas IFCO/HB")
    st.caption("Histórico, previsão de consumo e evolução do estoque por modelo de caixa")
    st.caption("Dashboard elaborado por Rafael Germano")

try:
    data = load_data()
except Exception as exc:
    st.error(f"Não foi possível carregar os dados: {exc}")
    st.stop()

packings = sorted(data["packing"].dropna().unique().tolist())
default_packing = packings[0] if packings else "Sem Packing"
packing = st.sidebar.selectbox("Cidade / Packing", packings, index=packings.index(default_packing) if packings else 0)
st.sidebar.caption("Packings da mesma cidade são agrupados em uma única opção.")

filtered = data[data["packing"] == packing].copy()
latest_data = filtered["data"].max().date() if not filtered.empty else date.today()
end_history = latest_data
start_history = end_history - timedelta(days=41)
history = filtered[(filtered["data"].dt.date >= start_history) & (filtered["data"].dt.date <= end_history)]
forecast_history = filtered.copy()

available = sorted(set(REQUIRED_MODELS) | set(filtered["modelo"].dropna().astype(str).unique()))
selected_models_labels = st.sidebar.multiselect(
    "Modelos de caixa",
    [MODEL_LABELS.get(model, f"Caixa {model}") for model in available],
    default=[MODEL_LABELS.get(model, f"Caixa {model}") for model in available],
)
selected_models = [LABEL_TO_MODEL.get(label, label) for label in selected_models_labels]
if not selected_models:
    st.warning("Selecione ao menos um modelo.")
    st.stop()

st.subheader("Histórico diário — últimas 6 semanas")
daily = (
    history[(history["modelo"].isin(selected_models)) & (history["data"].dt.weekday != 6)]
    .groupby(["data", "modelo"], as_index=False)["quantidade"]
    .sum()
)
history_table = (
    daily.pivot(index="data", columns="modelo", values="quantidade")
    .reindex(columns=selected_models, fill_value=0)
    .fillna(0)
    .sort_index(ascending=False)
)
history_table.columns = [MODEL_LABELS.get(str(valor), f"Caixa {valor}") for valor in history_table.columns]
history_table.index = [formatar_data(valor) for valor in history_table.index]
history_table.index.name = "data"
st.dataframe(
    history_table.style.format(formatar_numero),
    use_container_width=True,
)

forecast_days = operational_days(date.today(), 6)
forecast = model_forecast(
    forecast_history[forecast_history["modelo"].isin(selected_models)],
    selected_models,
    forecast_days,
)
collection_forecast_days = operational_days(date.today(), 8)
collection_forecast = model_forecast(
    forecast_history[forecast_history["modelo"].isin(selected_models)],
    selected_models,
    collection_forecast_days,
)
st.subheader("Previsão de consumo — hoje + próximos 5 dias operacionais")
st.caption(
    "Valores abaixo são a previsão operacional: previsão-base acrescida de buffer mínimo de 30% "
    "ou elevada ao percentil 75 dos consumos recentes do mesmo dia da semana, prevalecendo o maior valor."
)
forecast_table = (
    forecast.pivot(index="data", columns="modelo", values="previsao")
    .reindex(columns=selected_models, fill_value=0)
    .fillna(0)
    .sort_index(ascending=True)
)
forecast_table.columns = [MODEL_LABELS.get(str(valor), f"Caixa {valor}") for valor in forecast_table.columns]
forecast_table.index = [formatar_data(valor) for valor in forecast_table.index]
forecast_table.index.name = "data"
st.dataframe(
    forecast_table.style.format(formatar_numero),
    use_container_width=True,
)

st.subheader("Estoque atual e evolução projetada")
st.caption(
    "A evolução desconta a previsão operacional, já protegida contra picos. "
    "Se o saldo ficar negativo, é necessário programar reposição antes da data indicada."
)
stock_defaults = {model: 0.0 for model in selected_models}
input_cols = st.columns(min(5, len(selected_models)))
stock = {}
for index, model in enumerate(selected_models):
    with input_cols[index % len(input_cols)]:
        stock[model] = st.number_input(
            f"Estoque {MODEL_LABELS.get(model, f'Caixa {model}')}",
            min_value=0.0,
            value=stock_defaults[model],
            step=1.0,
            key=f"stock_{model}",
        )

forecast_numeric = (
    forecast.pivot(index="modelo", columns="data", values="previsao")
    .reindex(index=selected_models, fill_value=0)
    .fillna(0)
    .sort_index(axis=1, ascending=True)
)
evolution = forecast_numeric.copy()
for model in selected_models:
    consumo_acumulado = evolution.loc[model].cumsum()
    evolution.loc[model] = stock[model] - consumo_acumulado
evolution = evolution.T.sort_index(ascending=True)
evolution.columns = [MODEL_LABELS.get(str(valor), f"Caixa {valor}") for valor in evolution.columns]
evolution.index = [formatar_data(valor) for valor in evolution.index]
evolution.index.name = "data"
st.dataframe(
    evolution.style.map(destacar_negativo).format(formatar_numero),
    use_container_width=True,
)

st.subheader("Previsão de coleta de caixas")
st.caption(
    "Janelas de abastecimento: a coleta de segunda atende terça e quarta; "
    "a de quarta atende quinta e sexta; a de sexta atende sábado e segunda. "
    "As quantidades usam a previsão operacional com buffer de pico. "
    "A IFCO 6420 mantém estoque mínimo de 1.500 caixas."
)
coletas = calcular_coletas(collection_forecast, selected_models, stock)
coletas = coletas[coletas["data"].isin(forecast_days)]
if coletas.empty:
    st.info("Não há datas de coleta no horizonte selecionado.")
else:
    coletas = coletas.sort_values("data", ascending=True).set_index("data")
    coletas.columns = [MODEL_LABELS.get(str(valor), f"Caixa {valor}") for valor in coletas.columns]
    coletas.index = [formatar_data(valor) for valor in coletas.index]
    coletas.index.name = "data de coleta"
    st.dataframe(coletas.style.format(formatar_numero), use_container_width=True)

st.info(
    "A previsão-base usa o histórico completo da localidade e escolhe o método mais estável por tipo de caixa: "
    "mediana para 618 e 6424, média exponencial ponderada para 623 e 6416. A referência é o mesmo dia da semana "
    "ao longo do histórico, sem uso de informação futura. Para reduzir o risco de falta nos picos, a previsão "
    "operacional aplica buffer mínimo de 30% e cobertura pelo percentil 75 dos últimos 56 dias do mesmo dia da semana. "
    "Os domingos não entram no horizonte operacional, e valores negativos de estoque indicam possível ruptura de abastecimento."
)
st.caption("Tratamento aplicado: registro inconsistente de 10/09/2026 da HB 623 corrigido de 76875 para 76,875 caixas, conforme 1.230 kg / 16 unidades.")
