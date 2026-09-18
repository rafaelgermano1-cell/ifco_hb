# IFCO/HB - Previsão de consumo e estoque

Dashboard e pipeline de análise para consumo de caixas IFCO/HB por localidade,
packing e modelo de caixa.

## Funcionalidades

- normalização dos dados de vendas e consumo;
- preparação da base diária para previsão;
- validação temporal e comparação de modelos;
- filtro por localidade/packing e modelo de caixa;
- projeção de evolução de estoque;
- cálculo de necessidade de coleta;
- proteção contra picos de consumo por meio de buffer de segurança.

## Previsão operacional

A previsão-base usa o histórico completo disponível, sem utilizar informação
futura:

- modelos 618 e 6424: mediana do mesmo dia da semana;
- modelos 623 e 6416: média exponencial ponderada;
- demais modelos: mediana como comportamento padrão.

Para reduzir o risco de falta nos dias de pico, o dashboard aplica a maior
entre:

1. previsão-base acrescida de 30% de buffer mínimo; e
2. percentil 75 dos consumos dos últimos 56 dias do mesmo dia da semana.

O valor protegido é utilizado na evolução do estoque e na previsão de coleta.
Esse mecanismo reduz o risco de ruptura, mas não garante cobertura de eventos
excepcionais fora do padrão histórico.

## Localidade Araguari

Os packings abaixo são consolidados como `PH ARAGUARI`:

- `PH ARAGUARI - GRANEL`
- `PH ARAGUARI - EMBALADOS`

## Como executar

### Instalação

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### Dashboard

```powershell
streamlit run app.py
```

Também é possível executar o script `run_dashboard.ps1`.

## Organização principal

- `app.py`: dashboard Streamlit e lógica de previsão operacional;
- `normalizar_caixas.py`: normalização dos dados de origem;
- `preparar_base_previsao.py`: preparação da base diária;
- `avaliar_previsoes_caixas.py`: avaliação temporal das previsões;
- `avaliar_modelos_avancados.py`: comparação de modelos;
- `analisar_qualidade.py`: auditoria da qualidade dos dados;
- `dados_processados_completo/`: resultados processados e arquivos de apoio;
- `assets/`: recursos visuais do dashboard.

## Observações sobre os dados

Os arquivos Excel brutos e o ambiente virtual não são versionados neste
repositório por causa do tamanho. Eles devem ser mantidos localmente ou
armazenados em uma solução apropriada para dados grandes.
