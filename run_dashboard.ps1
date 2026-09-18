param(
    [int]$Port = 8501
)

python -m streamlit run app.py --server.port $Port
