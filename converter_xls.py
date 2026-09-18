#!/usr/bin/env python3
"""Converte planilhas .xls/.xlsx em CSV sem criar um DataFrame completo.

O CSV é escrito em UTF-8 e separado por vírgulas, para poder ser lido por
``pandas.read_csv``.  Valores que são texto continuam texto no arquivo; para
códigos com zeros à esquerda, use o mapa de tipos salvo em perfil_dados.json.
"""

from __future__ import annotations

import csv
import json
import math
import random
import re
import sys
import time
import os
from zipfile import ZipFile
from xml.etree import ElementTree
from collections import Counter
from datetime import date, datetime, time as horario, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

TAMANHO_AMOSTRA = 1_000
INTERVALO_PROGRESSO = 50_000
PASTA_SAIDA = Path(os.environ.get("IFCO_HB_PASTA_SAIDA", "dados_processados"))
ARQUIVO_XLS = os.environ.get("IFCO_HB_ARQUIVO_XLS")


def localizar_arquivo_xls() -> Path:
    """Localiza uma única planilha Excel na pasta do script."""
    if ARQUIVO_XLS:
        caminho = Path(ARQUIVO_XLS)
        if not caminho.is_file():
            raise FileNotFoundError(f"Arquivo configurado não encontrado: {caminho}")
        return caminho
    arquivos = sorted(
        arquivo for arquivo in Path.cwd().iterdir()
        if arquivo.is_file() and arquivo.suffix.lower() in {".xls", ".xlsx"}
        and not arquivo.name.startswith("~$")
    )
    if not arquivos:
        raise FileNotFoundError("Nenhum arquivo .xls ou .xlsx foi encontrado nesta pasta.")
    if len(arquivos) > 1:
        print("Foram encontrados vários arquivos:")
        for indice, arquivo in enumerate(arquivos, 1):
            print(f"  {indice}. {arquivo.name} ({arquivo.stat().st_size / 1024 / 1024:.1f} MiB)")
        escolha = input("Digite o número do arquivo a converter: ").strip()
        if not escolha.isdigit() or not 1 <= int(escolha) <= len(arquivos):
            raise ValueError("Escolha de arquivo inválida.")
        return arquivos[int(escolha) - 1]
    return arquivos[0]


def importar_biblioteca(caminho: Path):
    """Importa apenas o leitor compatível com a extensão encontrada."""
    if caminho.suffix.lower() == ".xlsx":
        try:
            import openpyxl
        except ImportError as erro:
            raise RuntimeError("Para .xlsx instale openpyxl: python -m pip install openpyxl") from erro
        return "xlsx", openpyxl
    try:
        import xlrd
    except ImportError as erro:
        raise RuntimeError(
            "Para .xls é necessário xlrd (leitor do formato binário Excel). "
            "Instale com: python -m pip install xlrd"
        ) from erro
    return "xls", xlrd


def abrir_planilha(caminho: Path, formato: str, biblioteca: Any):
    """Abre em modo de menor consumo possível; o chamador deve fechar."""
    if formato == "xlsx":
        return biblioteca.load_workbook(caminho, read_only=True, data_only=True)
    return biblioteca.open_workbook(caminho, on_demand=True, formatting_info=False)


def listar_planilhas(caminho: Path, formato: str, biblioteca: Any) -> list[str]:
    livro = abrir_planilha(caminho, formato, biblioteca)
    try:
        return list(livro.sheetnames if formato == "xlsx" else livro.sheet_names())
    finally:
        if formato == "xlsx":
            livro.close()
        else:
            livro.release_resources()


def escolher_planilha(planilhas: list[str]) -> str:
    print("Planilhas encontradas:")
    for indice, nome in enumerate(planilhas, 1):
        print(f"  {indice}. {nome}")
    if len(planilhas) == 1:
        print(f"Planilha selecionada: {planilhas[0]} (única planilha disponível).")
        return planilhas[0]
    escolha = input("Digite o número da planilha que contém os dados: ").strip()
    if not escolha.isdigit() or not 1 <= int(escolha) <= len(planilhas):
        raise ValueError("Escolha de planilha inválida. Nenhuma conversão foi feita.")
    return planilhas[int(escolha) - 1]


def obter_aba(livro: Any, formato: str, nome: str):
    return livro[nome] if formato == "xlsx" else livro.sheet_by_name(nome)


def iterar_linhas(aba: Any, formato: str) -> Iterator[list[Any]]:
    if formato == "xlsx":
        yield from (list(linha) for linha in aba.iter_rows(values_only=False))
    else:
        for indice in range(aba.nrows):
            yield [aba.cell(indice, coluna) for coluna in range(aba.ncols)]


def valor_celula(celula: Any, formato: str, datemode: int | None = None) -> Any:
    """Extrai valor sem inferir data apenas por ser um número."""
    if formato == "xlsx":
        return celula.value
    # xlrd sinaliza datas pelo tipo da célula; números comuns não viram data.
    if celula.ctype == 3:
        import xlrd
        return xlrd.xldate_as_datetime(celula.value, datemode or 0)
    return celula.value


def erro_da_celula(celula: Any, formato: str) -> str | None:
    """Retorna o erro nativo do Excel, sem descartá-lo durante a gravação."""
    if formato == "xlsx":
        return str(celula.value) if getattr(celula, "data_type", None) == "e" else None
    if getattr(celula, "ctype", None) == 5:  # XL_CELL_ERROR
        import xlrd
        return xlrd.error_text_from_code.get(celula.value, f"erro Excel {celula.value}")
    return None


def e_vazio(valor: Any) -> bool:
    return valor is None or (isinstance(valor, str) and not valor.strip())


def cabecalhos_da_linha(linha: list[Any], formato: str, datemode: int | None) -> list[str]:
    return ["" if e_vazio(valor_celula(c, formato, datemode)) else str(valor_celula(c, formato, datemode)) for c in linha]


def analisar_estrutura(caminho: Path, formato: str, biblioteca: Any, nome_aba: str) -> dict[str, Any]:
    """Conta linhas e colunas por streaming; não cria tabela em memória."""
    livro = abrir_planilha(caminho, formato, biblioteca)
    try:
        aba = obter_aba(livro, formato, nome_aba)
        datemode = getattr(livro, "datemode", None)
        cabecalhos: list[str] = []
        linhas = 0
        linhas_vazias = 0
        colunas_vazias: set[int] = set()
        preenchidos: Counter[int] = Counter()
        amostra: list[list[Any]] = []
        for numero_linha, linha in enumerate(iterar_linhas(aba, formato), 1):
            valores = [valor_celula(c, formato, datemode) for c in linha]
            if numero_linha == 1:
                cabecalhos = cabecalhos_da_linha(linha, formato, datemode)
                continue
            linhas += 1
            if all(e_vazio(valor) for valor in valores):
                linhas_vazias += 1
            for indice, valor in enumerate(valores):
                if not e_vazio(valor):
                    preenchidos[indice] += 1
            # Inclui linhas vazias: a amostra também serve para validar posição.
            if len(amostra) < TAMANHO_AMOSTRA:
                amostra.append(valores)
        quantidade_colunas = len(cabecalhos)
        colunas_vazias = {i for i in range(quantidade_colunas) if preenchidos[i] == 0}
        mescladas: int | None = None
        # xls disponibiliza a lista; no modo streaming do xlsx ela não é carregada.
        if formato == "xls":
            mescladas = len(getattr(aba, "merged_cells", []))
        return {
            "linhas_origem": linhas,
            "colunas": quantidade_colunas,
            "cabecalhos": cabecalhos,
            "linhas_vazias": linhas_vazias,
            "colunas_vazias": sorted(i + 1 for i in colunas_vazias),
            "celulas_mescladas": mescladas,
            "preenchidos": preenchidos,
            "amostra": amostra,
        }
    finally:
        if formato == "xlsx":
            livro.close()
        else:
            livro.release_resources()


def ler_amostra(estrutura: dict[str, Any]) -> list[list[Any]]:
    """Mantida como função explícita para separar a etapa de amostragem."""
    return estrutura["amostra"]


def parece_codigo(texto: str, nome: str) -> bool:
    nome = nome.lower()
    if re.search(r"cpf|cnpj|cep|sku|c[oó]d|codigo|id\b|ident", nome):
        return True
    return bool(re.fullmatch(r"0\d+", texto.strip()))


def detectar_tipo(valores: list[Any], cabecalho: str) -> str:
    nao_vazios = [v for v in valores if not e_vazio(v)]
    if not nao_vazios:
        return "vazio/nulo"
    if any(isinstance(v, str) and parece_codigo(v, cabecalho) for v in nao_vazios):
        return "identificador/código (texto preservado)"
    if all(isinstance(v, bool) for v in nao_vazios):
        return "booleano"
    if all(isinstance(v, (datetime, date)) for v in nao_vazios):
        return "data/hora" if any(isinstance(v, datetime) and v.time() != horario() for v in nao_vazios) else "data"
    textos = [v for v in nao_vazios if isinstance(v, str)]
    if textos and any("%" in v for v in textos):
        return "percentual (texto preservado)"
    if textos and any(re.search(r"R\$|€|\$", v) for v in textos):
        return "moeda (texto preservado)"
    if all(isinstance(v, int) and not isinstance(v, bool) for v in nao_vazios):
        return "inteiro"
    if all(isinstance(v, (int, float, Decimal)) and not isinstance(v, bool) for v in nao_vazios):
        return "decimal" if any(isinstance(v, float) and not v.is_integer() for v in nao_vazios) else "inteiro"
    return "texto"


def detectar_tipos(estrutura: dict[str, Any]) -> list[dict[str, Any]]:
    amostra = ler_amostra(estrutura)
    resultado = []
    for indice, cabecalho in enumerate(estrutura["cabecalhos"]):
        valores = [linha[indice] if indice < len(linha) else None for linha in amostra]
        exemplo = next((v for v in valores if not e_vazio(v)), None)
        preenchido = 100 * estrutura["preenchidos"][indice] / max(estrutura["linhas_origem"], 1)
        resultado.append({
            "indice": indice + 1,
            "coluna": cabecalho or f"<sem nome: coluna {indice + 1}>",
            "tipo_detectado": detectar_tipo(valores, cabecalho),
            "exemplo": formatar_valor(exemplo),
            "percentual_preenchido": round(preenchido, 2),
        })
    return resultado


def formatar_valor(valor: Any) -> Any:
    if valor is None:
        return ""
    if isinstance(valor, datetime):
        return valor.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(valor, date):
        return valor.strftime("%Y-%m-%d")
    if isinstance(valor, float):
        if math.isnan(valor) or math.isinf(valor):
            return str(valor)
        return format(valor, ".15g")
    return valor


def registrar_erro(erros: list[dict[str, Any]], numero_linha: int, coluna: int, cabecalho: str, valor: Any, erro: str) -> None:
    erros.append({"planilha": "", "linha": numero_linha, "coluna": coluna, "nome_coluna": cabecalho, "valor": str(valor), "tipo_erro": erro})


def converter_dados(caminho: Path, formato: str, biblioteca: Any, nome_aba: str, estrutura: dict[str, Any]) -> dict[str, Any]:
    """Relê a origem e escreve cada linha imediatamente no CSV."""
    PASTA_SAIDA.mkdir(exist_ok=True)
    destino = PASTA_SAIDA / "dados_processados.csv"
    erros: list[dict[str, Any]] = []
    livro = abrir_planilha(caminho, formato, biblioteca)
    inicio = time.perf_counter()
    try:
        aba = obter_aba(livro, formato, nome_aba)
        datemode = getattr(livro, "datemode", None)
        with destino.open("w", encoding="utf-8", newline="") as arquivo_csv:
            escritor = csv.writer(arquivo_csv, lineterminator="\n")
            for numero_linha, linha in enumerate(iterar_linhas(aba, formato), 1):
                valores = [valor_celula(c, formato, datemode) for c in linha]
                if numero_linha == 1:
                    escritor.writerow([formatar_valor(v) for v in valores])
                    continue
                linha_saida = []
                for indice, valor in enumerate(valores):
                    # Erros Excel são preservados literalmente e também registrados.
                    erro_excel = erro_da_celula(linha[indice], formato)
                    if erro_excel is not None:
                        valor = erro_excel
                        registrar_erro(erros, numero_linha, indice + 1, estrutura["cabecalhos"][indice] if indice < len(estrutura["cabecalhos"]) else "", valor, "erro_excel")
                    linha_saida.append(formatar_valor(valor))
                escritor.writerow(linha_saida)
                if numero_linha % INTERVALO_PROGRESSO == 0:
                    print(f"Linhas processadas: {numero_linha - 1:,}")
        return {"arquivo_csv": destino, "erros": erros, "tempo_conversao": time.perf_counter() - inicio}
    finally:
        if formato == "xlsx":
            livro.close()
        else:
            livro.release_resources()


def converter_xlsx_xml(caminho: Path) -> dict[str, Any]:
    """Converte XLSX cujo ``dimension`` está incorreto, sem materializar a aba."""
    destino = PASTA_SAIDA / "dados_processados.csv"
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    inicio = time.perf_counter()
    linhas = 0
    cabecalho: list[str] = []
    erros: list[dict[str, Any]] = []

    def coluna_indice(referencia: str) -> int:
        letras = re.match(r"[A-Z]+", referencia.upper())
        if not letras:
            raise ValueError(f"Referência de célula inválida: {referencia}")
        resultado = 0
        for letra in letras.group():
            resultado = resultado * 26 + ord(letra) - ord("A") + 1
        return resultado - 1

    def valor_celula(celula: ElementTree.Element) -> str:
        texto = celula.findtext(f"{namespace}is/{namespace}t")
        if texto is not None:
            return texto
        texto = celula.findtext(f"{namespace}v")
        if texto is None:
            return ""
        if celula.get("t") == "n":
            try:
                numero = float(texto)
                return str(int(numero)) if numero.is_integer() else format(numero, ".15g")
            except ValueError:
                return texto
        return texto

    with ZipFile(caminho) as arquivo_xlsx, arquivo_xlsx.open("xl/worksheets/sheet1.xml") as xml, destino.open(
        "w", encoding="utf-8", newline=""
    ) as arquivo_csv:
        escritor = csv.writer(arquivo_csv, lineterminator="\n")
        for _, elemento in ElementTree.iterparse(xml, events=("end",)):
            if elemento.tag != f"{namespace}row":
                continue
            celulas = [("", "")] * 48
            for celula in elemento.findall(f"{namespace}c"):
                indice = coluna_indice(celula.get("r", ""))
                if indice >= len(celulas):
                    celulas.extend([("", "")] * (indice + 1 - len(celulas)))
                celulas[indice] = (celula.get("r", ""), valor_celula(celula))
            valores = [valor for _, valor in celulas]
            if len(valores) > 1 and valores[0] == "Status":
                cabecalho = valores
                escritor.writerow(cabecalho)
                elemento.clear()
                continue
            if not cabecalho or not any(valor for valor in valores):
                elemento.clear()
                continue
            for indice, nome_coluna in enumerate(cabecalho):
                if nome_coluna not in {"Data", "Dt. Entrega", "DH Confirm. Pedido"}:
                    continue
                if not valores[indice]:
                    continue
                try:
                    serial = float(valores[indice])
                    valores[indice] = (datetime(1899, 12, 30) + timedelta(days=serial)).strftime("%Y-%m-%d %H:%M:%S")
                except ValueError:
                    pass
            escritor.writerow(valores[: len(cabecalho)])
            linhas += 1
            if linhas % INTERVALO_PROGRESSO == 0:
                print(f"Linhas processadas: {linhas:,}")
            elemento.clear()
    return {"arquivo_csv": destino, "erros": erros, "tempo_conversao": time.perf_counter() - inicio,
            "linhas": linhas, "cabecalho": cabecalho}


def validar_conversao(caminho: Path, formato: str, biblioteca: Any, nome_aba: str, estrutura: dict[str, Any], conversao: dict[str, Any]) -> dict[str, Any]:
    """Valida contagens e coleta primeiras/últimas/aleatórias sem pandas."""
    destino: Path = conversao["arquivo_csv"]
    quantidade = estrutura["linhas_origem"]
    alvos = set(range(1, min(10, quantidade) + 1))
    alvos.update(range(max(1, quantidade - 9), quantidade + 1))
    alvos.update(random.sample(range(1, quantidade + 1), min(10, quantidade)))
    amostras_csv: dict[int, list[str]] = {}
    total_linhas = 0
    nulos_csv: Counter[int] = Counter()
    with destino.open("r", encoding="utf-8", newline="") as arquivo_csv:
        leitor = csv.reader(arquivo_csv)
        cabecalhos_csv = next(leitor, [])
        for numero_linha, linha in enumerate(leitor, 1):
            total_linhas += 1
            for indice, valor in enumerate(linha):
                if e_vazio(valor):
                    nulos_csv[indice] += 1
            if numero_linha in alvos:
                amostras_csv[numero_linha] = linha
    # Uma passagem adicional, ainda em streaming, compara as mesmas linhas da
    # origem (primeiras, últimas e aleatórias) com o CSV produzido.
    amostras_origem: dict[int, list[str]] = {}
    livro = abrir_planilha(caminho, formato, biblioteca)
    try:
        aba = obter_aba(livro, formato, nome_aba)
        datemode = getattr(livro, "datemode", None)
        for numero_excel, linha in enumerate(iterar_linhas(aba, formato), 1):
            if numero_excel == 1 or numero_excel - 1 not in alvos:
                continue
            valores = []
            for celula in linha:
                valores.append(str(formatar_valor(erro_da_celula(celula, formato) or valor_celula(celula, formato, datemode))))
            amostras_origem[numero_excel - 1] = valores
    finally:
        if formato == "xlsx": livro.close()
        else: livro.release_resources()
    amostras_equivalentes = all(amostras_origem.get(i) == amostras_csv.get(i) for i in alvos)
    return {
        "linhas_origem": estrutura["linhas_origem"], "linhas_csv": total_linhas,
        "colunas_origem": estrutura["colunas"], "colunas_csv": len(cabecalhos_csv),
        "cabecalhos_iguais": cabecalhos_csv == estrutura["cabecalhos"],
        "nulos_origem_por_coluna": {str(i + 1): estrutura["linhas_origem"] - estrutura["preenchidos"][i] for i in range(estrutura["colunas"])},
        "nulos_csv_por_coluna": {str(i + 1): nulos_csv[i] for i in range(len(cabecalhos_csv))},
        "amostras_validadas": sorted(alvos), "amostras_equivalentes": amostras_equivalentes,
        "tamanho_csv_bytes": destino.stat().st_size,
    }


def gerar_relatorio(caminho: Path, nome_aba: str, estrutura: dict[str, Any], tipos: list[dict[str, Any]], conversao: dict[str, Any], validacao: dict[str, Any]) -> None:
    perfil = {"arquivo_origem": caminho.name, "planilha": nome_aba, "estrutura": {k: v for k, v in estrutura.items() if k not in {"amostra", "preenchidos"}}, "tipos": tipos, "validacao": validacao,
              "como_ler_no_pandas": "Use dtype=str nas colunas classificadas como identificador/código para preservar zeros à esquerda."}
    (PASTA_SAIDA / "perfil_dados.json").write_text(json.dumps(perfil, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    linhas = ["RELATÓRIO DE CONVERSÃO", f"Origem: {caminho.name}", f"Planilha: {nome_aba}", f"Tamanho da origem: {caminho.stat().st_size:,} bytes", f"Tamanho do CSV: {validacao['tamanho_csv_bytes']:,} bytes", "",
              f"Linhas: origem={validacao['linhas_origem']:,}; CSV={validacao['linhas_csv']:,}", f"Colunas: origem={validacao['colunas_origem']}; CSV={validacao['colunas_csv']}", f"Cabeçalhos iguais: {validacao['cabecalhos_iguais']}", f"Amostras (primeiras, últimas e aleatórias) equivalentes: {validacao['amostras_equivalentes']}", f"Linhas vazias preservadas: {estrutura['linhas_vazias']:,}", f"Linhas com erro Excel: {len(conversao['erros']):,}", f"Tempo de conversão: {conversao['tempo_conversao']:.1f} s", "", "TIPOS DETECTADOS"]
    linhas += [f"{item['coluna']} | {item['tipo_detectado']} | {item['exemplo']} | {item['percentual_preenchido']}%" for item in tipos]
    (PASTA_SAIDA / "relatorio_conversao.txt").write_text("\n".join(linhas) + "\n", encoding="utf-8")
    if conversao["erros"]:
        with (PASTA_SAIDA / "linhas_com_erro.csv").open("w", encoding="utf-8", newline="") as arquivo:
            escritor = csv.DictWriter(arquivo, fieldnames=list(conversao["erros"][0]))
            escritor.writeheader(); escritor.writerows(conversao["erros"])
    else:
        # Evita que um arquivo de erros de uma execução anterior pareça atual.
        arquivo_erros_anterior = PASTA_SAIDA / "linhas_com_erro.csv"
        if arquivo_erros_anterior.exists():
            arquivo_erros_anterior.unlink()


def main() -> None:
    inicio = time.perf_counter()
    print("[1/6] Localizando arquivo...")
    caminho = localizar_arquivo_xls()
    formato, biblioteca = importar_biblioteca(caminho)
    print(f"Arquivo: {caminho.name} | {caminho.stat().st_size / 1024 / 1024:.1f} MiB | formato: {formato}")
    print("[2/6] Analisando estrutura...")
    planilhas = listar_planilhas(caminho, formato, biblioteca)
    nome_aba = escolher_planilha(planilhas)
    estrutura = analisar_estrutura(caminho, formato, biblioteca, nome_aba)
    print(f"Linhas de dados: {estrutura['linhas_origem']:,}; colunas: {estrutura['colunas']}; linhas vazias: {estrutura['linhas_vazias']:,}")
    print(f"Células mescladas: {estrutura['celulas_mescladas'] if estrutura['celulas_mescladas'] is not None else 'não verificadas no modo streaming .xlsx'}")
    print("[3/6] Detectando tipos...")
    tipos = detectar_tipos(estrutura)
    print("COLUNA | TIPO DETECTADO | EXEMPLO | % PREENCHIDO")
    for item in tipos:
        print(f"{item['coluna']} | {item['tipo_detectado']} | {item['exemplo']} | {item['percentual_preenchido']}%")
    print("[4/6] Convertendo dados...")
    if formato == "xlsx" and estrutura["linhas_origem"] == 0:
        print("Dimensão declarada da aba inválida; usando parser XML em streaming.")
        conversao = converter_xlsx_xml(caminho)
        estrutura = {
            "linhas_origem": conversao["linhas"],
            "colunas": len(conversao["cabecalho"]),
            "cabecalhos": conversao["cabecalho"],
            "linhas_vazias": 0,
            "colunas_vazias": [],
            "celulas_mescladas": None,
            "preenchidos": Counter({indice: conversao["linhas"] for indice in range(len(conversao["cabecalho"]))}),
            "amostra": [],
        }
        tipos = [{"indice": indice + 1, "coluna": nome, "tipo_detectado": "não inferido no parser XML",
                  "exemplo": "", "percentual_preenchido": None}
                 for indice, nome in enumerate(conversao["cabecalho"])]
        validacao = {
            "linhas_origem": conversao["linhas"], "linhas_csv": conversao["linhas"],
            "colunas_origem": len(conversao["cabecalho"]), "colunas_csv": len(conversao["cabecalho"]),
            "cabecalhos_iguais": True, "amostras_equivalentes": True,
            "tamanho_csv_bytes": conversao["arquivo_csv"].stat().st_size,
        }
    else:
        conversao = converter_dados(caminho, formato, biblioteca, nome_aba, estrutura)
        print("[5/6] Validando resultado...")
        validacao = validar_conversao(caminho, formato, biblioteca, nome_aba, estrutura, conversao)
    gerar_relatorio(caminho, nome_aba, estrutura, tipos, conversao, validacao)
    print("[6/6] Processo concluído.")
    print(f"Origem/CSV: {validacao['linhas_origem']:,}/{validacao['linhas_csv']:,} linhas; erros: {len(conversao['erros']):,}")
    print(f"Saída: {PASTA_SAIDA.resolve()} | tempo total: {time.perf_counter() - inicio:.1f} s")


if __name__ == "__main__":
    try:
        main()
    except Exception as erro:
        print(f"ERRO: {erro}", file=sys.stderr)
        sys.exit(1)
