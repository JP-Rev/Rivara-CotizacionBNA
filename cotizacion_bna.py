import os
import re
import smtplib
import subprocess
import sys
import tempfile
import traceback

from datetime import datetime
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

import requests


# =============================================================================
# CONFIGURACIÓN (.env)
# =============================================================================
#
# Toda la configuración se lee de un archivo .env ubicado al lado de este
# .py (ver .env.example). No hace falta instalar python-dotenv.
#
# Prioridad de los valores:
#   1. Variables de entorno reales del sistema
#   2. Archivo .env
#   3. Valores por defecto definidos más abajo
#
# IMPORTANTE: el .env NO debe subirse nunca a un repositorio.

RUTA_BASE = Path(__file__).resolve().parent

ARCHIVO_ENV = RUTA_BASE / ".env"


def cargar_env(ruta):
    """
    Lee un archivo .env simple (NOMBRE=valor) y lo devuelve como dict.

    No admite comentarios al final de la línea: todo lo que está
    después del '=' se toma como valor (para no romper contraseñas
    que contengan el carácter '#').
    """

    valores = {}

    if not ruta.exists():
        return valores

    contenido = ruta.read_text(encoding="utf-8-sig")

    for numero, linea in enumerate(contenido.splitlines(), start=1):
        linea = linea.strip()

        if not linea or linea.startswith("#"):
            continue

        if linea.lower().startswith("export "):
            linea = linea[len("export "):].strip()

        if "=" not in linea:
            raise RuntimeError(
                f"Línea {numero} del archivo .env sin '=': {linea!r}"
            )

        nombre, _, valor = linea.partition("=")

        nombre = nombre.strip()
        valor = valor.strip()

        # Quita comillas envolventes si las hay.
        if (
            len(valor) >= 2
            and valor[0] == valor[-1]
            and valor[0] in ("'", '"')
        ):
            valor = valor[1:-1]

        valores[nombre] = valor

    return valores


_ENV = cargar_env(ARCHIVO_ENV)


def env(nombre, defecto=""):
    valor = os.environ.get(nombre)

    if valor is None:
        valor = _ENV.get(nombre)

    if valor is None or valor.strip() == "":
        return defecto

    return valor.strip()


def env_int(nombre, defecto):
    valor = env(nombre, None)

    if valor is None:
        return defecto

    try:
        return int(valor)
    except ValueError as error:
        raise RuntimeError(
            f"{nombre} debe ser un número entero. "
            f"Valor configurado: {valor!r}"
        ) from error


def env_bool(nombre, defecto):
    valor = env(nombre, None)

    if valor is None:
        return defecto

    normalizado = valor.lower()

    if normalizado in ("1", "true", "si", "sí", "s", "yes", "y"):
        return True

    if normalizado in ("0", "false", "no", "n"):
        return False

    raise RuntimeError(
        f"{nombre} debe ser true o false. "
        f"Valor configurado: {valor!r}"
    )


def env_lista(nombre, defecto):
    valor = env(nombre, None)

    if valor is None:
        return list(defecto)

    elementos = [
        elemento.strip()
        for elemento in valor.split(",")
        if elemento.strip()
    ]

    if not elementos:
        return list(defecto)

    return elementos


def env_monedas(nombre, defecto):
    """
    Convierte "D=venta,C=compra" en {"D": "venta", "C": "compra"}.
    """

    valor = env(nombre, None)

    if valor is None:
        return dict(defecto)

    monedas = {}

    for par in valor.split(","):
        par = par.strip()

        if not par:
            continue

        if "=" not in par:
            raise RuntimeError(
                f"{nombre} debe tener el formato "
                f"\"D=venta,C=compra\". Par inválido: {par!r}"
            )

        codigo, _, tipo = par.partition("=")

        codigo = codigo.strip().upper()
        tipo = tipo.strip().lower()

        if tipo not in ("compra", "venta"):
            raise RuntimeError(
                f"{nombre}: el valor para el código {codigo!r} debe "
                f"ser \"compra\" o \"venta\". Recibido: {tipo!r}"
            )

        monedas[codigo] = tipo

    if not monedas:
        return dict(defecto)

    return monedas


# =============================================================================
# LOG
# =============================================================================

# Carpeta donde se guardan los logs.
# Por defecto, una subcarpeta "Logs" al lado de este mismo .py.
LOG_DIR = Path(
    env("LOG_DIR", str(RUTA_BASE / "Logs"))
)


class _Tee:
    """
    Duplica todo lo que se escribe en consola (print, errores)
    hacia un archivo de log, agregando fecha y hora a cada línea.
    """

    def __init__(self, original, archivo):
        self.original = original
        self.archivo = archivo
        self._inicio_linea = True

    def write(self, texto):
        if self.original is not None:
            try:
                self.original.write(texto)
            except Exception:
                pass

        for parte in texto.splitlines(keepends=True):
            if self._inicio_linea:
                self.archivo.write(
                    f"[{datetime.now():%d/%m/%Y %H:%M:%S}] "
                )
            self.archivo.write(parte)
            self._inicio_linea = parte.endswith("\n")

        self.archivo.flush()
        return len(texto)

    def flush(self):
        if self.original is not None:
            try:
                self.original.flush()
            except Exception:
                pass
        self.archivo.flush()


# =============================================================================
# BANCO DE LA NACIÓN ARGENTINA (BNA)
# =============================================================================

URL_BNA = env("URL_BNA", "https://www.bna.com.ar/Personas")

# Solapa de la página del BNA de donde se toma el valor.
#   "divisas"  -> Divisas (es el valor que publicaba Errepar: divisaVenta)
#   "billetes" -> Billetes (pizarra mostrador)
TIPO_COTIZACION = env("TIPO_COTIZACION", "divisas").lower()

# Qué códigos de MCZ_MONEDA se actualizan y con qué valor del BNA.
# Según la tabla MONEDAS del ERP:
#   D = "DOLARES"         -> se carga la cotización de VENTA
#   C = "Dolar comprador" -> se carga la cotización de COMPRA
# Para volver al comportamiento anterior (solo venta), dejá únicamente
# la entrada "D".
MONEDAS_A_ACTUALIZAR = env_monedas(
    "MONEDAS_A_ACTUALIZAR",
    {
        "D": "venta",
        "C": "compra",
    },
)

# Texto de la fila de la tabla del BNA que corresponde al dólar.
MONEDA_BUSCADA = env("MONEDA_BUSCADA", "dolar u.s.a").lower()

# Si es True, el proceso aborta cuando la fecha publicada por el BNA
# no es la del día (evita grabar en el ERP la cotización de ayer).
EXIGIR_FECHA_ACTUAL = env_bool("EXIGIR_FECHA_ACTUAL", True)

INTENTOS_HTTP = env_int("INTENTOS_HTTP", 3)

if TIPO_COTIZACION not in ("divisas", "billetes"):
    raise RuntimeError(
        "TIPO_COTIZACION debe ser \"divisas\" o \"billetes\". "
        f"Valor configurado: {TIPO_COTIZACION!r}"
    )


# =============================================================================
# ORACLE / SQL*PLUS
# =============================================================================

SQLPLUS_EXE = Path(
    env(
        "SQLPLUS_EXE",
        r"C:\oracle\product\client\bin\sqlplus.exe",
    )
)

ORACLE_USUARIO = env("ORACLE_USUARIO", "")
ORACLE_PASSWORD = env("ORACLE_PASSWORD", "")
ORACLE_ALIAS = env("ORACLE_ALIAS", "")

ESQUEMAS_ORACLE = env_lista("ESQUEMAS_ORACLE", [])


# =============================================================================
# SMTP
# =============================================================================

SMTP_SERVER = env("SMTP_SERVER", "smtp.office365.com")
SMTP_PORT = env_int("SMTP_PORT", 587)

SMTP_USER = env("SMTP_USER", "")
SMTP_PASS = env("SMTP_PASS", "")


TO_EMAIL = env_lista("TO_EMAIL", [])

FROM_EMAIL = env("FROM_EMAIL", SMTP_USER)

EMAIL_SUBJECT = env(
    "EMAIL_SUBJECT",
    "Error al actualizar cotización del dólar",
)

# Se activa con --solo-consultar: no escribe en Oracle ni envía mails.
MODO_PRUEBA = False


# =============================================================================
# ENVIAR CORREO DE ERROR
# =============================================================================

def enviar_mail_error(error):
    if MODO_PRUEBA:
        print(
            "Modo --solo-consultar: no se envía correo de error."
        )
        return

    if not SMTP_USER or not SMTP_PASS or not TO_EMAIL:
        print(
            "AVISO: no se envía el correo de error porque faltan "
            "SMTP_USER, SMTP_PASS o TO_EMAIL en el archivo .env"
        )
        return

    mensaje = EmailMessage()

    mensaje["From"] = FROM_EMAIL
    mensaje["To"] = ", ".join(TO_EMAIL)
    mensaje["Subject"] = EMAIL_SUBJECT

    detalle_error = traceback.format_exc()

    cuerpo = f"""
Se produjo un error durante la actualización automática de la cotización
del dólar en Oracle.

Fuente de datos:
{URL_BNA} (solapa {TIPO_COTIZACION})

Fecha y hora:
{datetime.now():%d/%m/%Y %H:%M:%S}

Directorio de ejecución:
{Path.cwd()}

Tipo de error:
{type(error).__name__}

Descripción:
{error}

Detalle técnico:
{detalle_error}
""".strip()

    mensaje.set_content(cuerpo)

    try:
        with smtplib.SMTP(
            SMTP_SERVER,
            SMTP_PORT,
            timeout=30,
        ) as servidor:

            servidor.ehlo()
            servidor.starttls()
            servidor.ehlo()

            servidor.login(
                SMTP_USER,
                SMTP_PASS,
            )

            servidor.send_message(mensaje)

        print("Correo de error enviado correctamente.")

    except Exception as error_mail:
        print(
            "No se pudo enviar el correo de error:",
            error_mail,
        )


# =============================================================================
# PARSEO DE TABLAS HTML (sin dependencias externas)
# =============================================================================

class _LectorTablas(HTMLParser):
    """
    Extrae las tablas de un fragmento HTML como listas de filas,
    y cada fila como lista de textos de celda.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)

        self.tablas = []

        self._tabla = None
        self._fila = None
        self._celda = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._tabla = []

        elif tag == "tr" and self._tabla is not None:
            self._fila = []

        elif tag in ("td", "th") and self._fila is not None:
            self._celda = []

        elif tag == "br" and self._celda is not None:
            self._celda.append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._celda is not None:
            texto = " ".join("".join(self._celda).split())
            self._fila.append(texto)
            self._celda = None

        elif tag == "tr" and self._fila is not None:
            if self._celda is not None:
                texto = " ".join("".join(self._celda).split())
                self._fila.append(texto)
                self._celda = None

            self._tabla.append(self._fila)
            self._fila = None

        elif tag == "table" and self._tabla is not None:
            self.tablas.append(self._tabla)
            self._tabla = None

    def handle_data(self, data):
        if self._celda is not None:
            self._celda.append(data)


def _recortar_solapa(html, id_solapa):
    """
    Devuelve el fragmento de HTML correspondiente al div de la solapa
    pedida (por ejemplo id="divisas"), cortando antes del div siguiente.
    """

    patron_inicio = re.compile(
        r"<div[^>]*\bid\s*=\s*[\"']" + re.escape(id_solapa) + r"[\"']",
        re.IGNORECASE,
    )

    coincidencia = patron_inicio.search(html)

    if coincidencia is None:
        raise RuntimeError(
            "No se encontró en la página del BNA la solapa "
            f"id=\"{id_solapa}\". Es probable que el sitio haya "
            "cambiado su estructura."
        )

    fragmento = html[coincidencia.end():]

    # Corta en el inicio del siguiente panel de cotizaciones,
    # para no mezclar Billetes con Divisas.
    patron_fin = re.compile(
        r"<div[^>]*\bid\s*=\s*[\"']"
        r"(billetes|divisas|titulos|cheques|monedas)[\"']",
        re.IGNORECASE,
    )

    fin = patron_fin.search(fragmento)

    if fin is not None:
        fragmento = fragmento[:fin.start()]

    return fragmento


def _a_decimal(texto, descripcion):
    """
    Convierte el formato del BNA (1.510,5000) a Decimal.
    Soporta además 1510,50 / 1510.50 / 1510.
    """

    valor = unescape(str(texto)).strip()
    valor = valor.replace("$", "").replace(" ", "")

    if "," in valor:
        valor_normalizado = valor.replace(".", "").replace(",", ".")
    else:
        valor_normalizado = valor

    try:
        numero = Decimal(valor_normalizado)
    except InvalidOperation as error:
        raise RuntimeError(
            f"El valor de {descripcion} no tiene un formato válido: "
            f"{texto!r}"
        ) from error

    return numero


# =============================================================================
# OBTENER COTIZACIÓN DEL BNA
# =============================================================================

PATRON_FECHA = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")


def _extraer_fecha(tabla, fragmento):
    """
    Busca la fecha de la cotización.

    El BNA la publica en la cabecera de la tabla con día y mes de uno
    o dos dígitos (por ejemplo 4/11/2026), así que primero se busca
    dentro de las celdas de la tabla; recién si no aparece ahí se
    recorre el resto del fragmento. Buscar en la tabla evita agarrar
    por error cualquier otra fecha suelta de la página.
    """

    candidatas = []

    for fila in tabla:
        for celda in fila:
            coincidencia = PATRON_FECHA.search(celda)

            if coincidencia is not None:
                candidatas.append(coincidencia.group(0))

    origen = "la cabecera de la tabla"

    if not candidatas:
        coincidencia = PATRON_FECHA.search(fragmento)

        if coincidencia is not None:
            candidatas.append(coincidencia.group(0))
            origen = "el cuerpo de la página"

    if not candidatas:
        raise RuntimeError(
            "No se pudo leer la fecha de cotización publicada "
            "por el BNA."
        )

    fecha_texto = candidatas[0]

    try:
        fecha = datetime.strptime(fecha_texto, "%d/%m/%Y")
    except ValueError as error:
        raise RuntimeError(
            "La fecha publicada por el BNA no es válida: "
            f"{fecha_texto}"
        ) from error

    print(f"Fecha leída de {origen}: {fecha_texto}")

    return fecha


def descargar_html_bna():
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "es-AR,es;q=0.9",
    }

    ultimo_error = None

    for intento in range(1, INTENTOS_HTTP + 1):
        try:
            print(
                f"Consultando cotización del BNA "
                f"(intento {intento} de {INTENTOS_HTTP})..."
            )

            respuesta = requests.get(
                URL_BNA,
                headers=headers,
                timeout=30,
            )

            respuesta.raise_for_status()

            # El sitio del BNA no siempre declara bien el encoding.
            if not respuesta.encoding:
                respuesta.encoding = "utf-8"

            return respuesta.text

        except requests.RequestException as error:
            ultimo_error = error

            print(
                f"Falló el intento {intento}: "
                f"{type(error).__name__}: {error}"
            )

    raise ultimo_error


def obtener_cotizacion_bna():
    html = descargar_html_bna()

    fragmento = _recortar_solapa(html, TIPO_COTIZACION)

    lector = _LectorTablas()
    lector.feed(fragmento)

    if not lector.tablas:
        raise RuntimeError(
            "No se encontraron tablas de cotizaciones en la solapa "
            f"\"{TIPO_COTIZACION}\" del BNA."
        )

    tabla = lector.tablas[0]

    # ---------------------------------------------------------------
    # Fecha publicada por el BNA
    # ---------------------------------------------------------------

    fecha_bna = _extraer_fecha(tabla, fragmento)

    # ---------------------------------------------------------------
    # Fila del dólar
    # ---------------------------------------------------------------

    fila_dolar = None

    for fila in tabla:
        if not fila:
            continue

        etiqueta = unescape(fila[0]).strip().lower()
        etiqueta = etiqueta.replace("ó", "o").replace("á", "a")

        if etiqueta.startswith(MONEDA_BUSCADA):
            fila_dolar = fila
            break

    if fila_dolar is None:
        raise RuntimeError(
            "No se encontró la fila del dólar "
            f"(\"{MONEDA_BUSCADA}\") en la tabla de "
            f"{TIPO_COTIZACION} del BNA. Filas leídas: {tabla!r}"
        )

    if len(fila_dolar) < 3:
        raise RuntimeError(
            "La fila del dólar no tiene las columnas esperadas "
            f"(Moneda / Compra / Venta): {fila_dolar!r}"
        )

    compra_texto = fila_dolar[1]
    venta_texto = fila_dolar[2]

    compra = _a_decimal(compra_texto, "divisa compra")
    venta = _a_decimal(venta_texto, "divisa venta")

    print(f"Fecha publicada por el BNA: {fecha_bna:%d/%m/%Y}")
    print(f"Solapa utilizada: {TIPO_COTIZACION}")
    print(f"Dólar compra (original): {compra_texto!r} -> {compra}")
    print(f"Dólar venta  (original): {venta_texto!r} -> {venta}")

    # ---------------------------------------------------------------
    # Validaciones
    # ---------------------------------------------------------------

    fecha_actual = datetime.now().date()

    if fecha_bna.date() > fecha_actual:
        raise RuntimeError(
            "La fecha publicada por el BNA es futura. "
            f"Fecha del BNA: {fecha_bna:%d/%m/%Y}. "
            f"Fecha actual: {fecha_actual:%d/%m/%Y}. "
            "No se actualizó ninguna tabla."
        )

    if EXIGIR_FECHA_ACTUAL and fecha_bna.date() != fecha_actual:
        raise RuntimeError(
            "La cotización obtenida no corresponde al día actual. "
            f"Fecha del BNA: {fecha_bna:%d/%m/%Y}. "
            f"Fecha actual: {fecha_actual:%d/%m/%Y}. "
            "No se actualizó ninguna tabla."
        )

    if compra <= 0 or venta <= 0:
        raise RuntimeError(
            "El BNA devolvió una cotización no válida. "
            f"Compra: {compra}. Venta: {venta}."
        )

    if venta < compra:
        raise RuntimeError(
            "La cotización de venta es menor que la de compra; "
            "probablemente las columnas se leyeron invertidas. "
            f"Compra: {compra}. Venta: {venta}."
        )

    # MCZ_COTIZACION es NUMBER(11,4): admite hasta 9.999.999,9999.
    limite_oracle = Decimal("9999999.9999")

    valores = {
        "compra": compra,
        "venta": venta,
    }

    for nombre, valor in valores.items():
        if valor > limite_oracle:
            raise RuntimeError(
                "La cotización excede la capacidad de "
                "MCZ_COTIZACION NUMBER(11,4). "
                f"Valor de {nombre}: {valor}."
            )

        # Control práctico para detectar formatos inesperados.
        if valor > Decimal("100000"):
            raise RuntimeError(
                "La cotización recibida parece incorrecta o mal "
                f"convertida. Valor de {nombre}: {valor}."
            )

    # Arma el detalle final: código de moneda del ERP -> cotización.
    cotizaciones = {}

    for codigo_moneda, tipo_valor in MONEDAS_A_ACTUALIZAR.items():
        if tipo_valor not in valores:
            raise RuntimeError(
                "MONEDAS_A_ACTUALIZAR solo admite \"compra\" o "
                f"\"venta\". Valor configurado para el código "
                f"{codigo_moneda!r}: {tipo_valor!r}"
            )

        cotizaciones[codigo_moneda] = valores[tipo_valor]

        print(
            f"Se guardará en MCZ_MONEDA = {codigo_moneda!r} "
            f"({tipo_valor}): {valores[tipo_valor]}"
        )

    if not cotizaciones:
        raise RuntimeError(
            "MONEDAS_A_ACTUALIZAR está vacío: no hay nada que "
            "actualizar."
        )

    return fecha_bna, cotizaciones


# =============================================================================
# GENERAR SQL PARA CADA ESQUEMA
# =============================================================================

def generar_merge_esquema(
    esquema,
    codigo_moneda,
    fecha_sql,
    cotizacion_sql,
):
    return f"""
PROMPT Actualizando {esquema}.MONEDAS_COTIZACIONES (moneda {codigo_moneda})...

MERGE INTO {esquema}.MONEDAS_COTIZACIONES destino
USING (
    SELECT
        '{codigo_moneda}' AS moneda,
        TO_DATE(
            '{fecha_sql}',
            'DD/MM/YYYY'
        ) AS fecha,
        TO_NUMBER(
            '{cotizacion_sql}',
            '999999999999990D9999',
            'NLS_NUMERIC_CHARACTERS='',.'''
        ) AS cotizacion
    FROM dual
) origen
ON (
       destino.MCZ_MONEDA = origen.moneda
   AND destino.MCZ_FECHA >= origen.fecha
   AND destino.MCZ_FECHA < origen.fecha + 1
)
WHEN MATCHED THEN
    UPDATE SET
        destino.MCZ_COTIZACION = origen.cotizacion
WHEN NOT MATCHED THEN
    INSERT (
        MCZ_MONEDA,
        MCZ_FECHA,
        MCZ_COTIZACION
    )
    VALUES (
        origen.moneda,
        origen.fecha,
        origen.cotizacion
    );

PROMPT {esquema}_{codigo_moneda}_ACTUALIZADO
"""


# =============================================================================
# GENERAR SCRIPT SQL COMPLETO
# =============================================================================

def generar_sql(fecha, cotizaciones):
    fecha_sql = fecha.strftime(
        "%d/%m/%Y"
    )

    bloques = []

    for esquema in ESQUEMAS_ORACLE:
        for codigo_moneda, cotizacion in cotizaciones.items():

            # Decimal("1551.0000") -> "1551,0000"
            cotizacion_sql = (
                format(cotizacion, ".4f")
                .replace(".", ",")
            )

            bloques.append(
                generar_merge_esquema(
                    esquema,
                    codigo_moneda,
                    fecha_sql,
                    cotizacion_sql,
                )
            )

    operaciones = "\n".join(bloques)

    return f"""
WHENEVER OSERROR EXIT 10
WHENEVER SQLERROR EXIT SQL.SQLCODE ROLLBACK

SET ECHO OFF
SET FEEDBACK ON
SET VERIFY OFF
SET HEADING OFF
SET SERVEROUTPUT ON
SET SQLBLANKLINES ON

ALTER SESSION SET NLS_NUMERIC_CHARACTERS = ',.';

{operaciones}

COMMIT;

PROMPT TODAS_LAS_COTIZACIONES_GUARDADAS

EXIT 0
"""


# =============================================================================
# GUARDAR EN ORACLE
# =============================================================================

def guardar_en_oracle(
    fecha,
    cotizaciones,
):
    faltantes = [
        nombre
        for nombre, valor in (
            ("ORACLE_USUARIO", ORACLE_USUARIO),
            ("ORACLE_PASSWORD", ORACLE_PASSWORD),
            ("ORACLE_ALIAS", ORACLE_ALIAS),
            ("ESQUEMAS_ORACLE", ESQUEMAS_ORACLE),
        )
        if not valor
    ]

    if faltantes:
        raise RuntimeError(
            "Falta configurar en el archivo .env "
            f"({ARCHIVO_ENV}): "
            + ", ".join(faltantes)
        )

    if not SQLPLUS_EXE.exists():
        raise FileNotFoundError(
            "No se encontró SQL*Plus en: "
            f"{SQLPLUS_EXE}"
        )

    contenido_sql = generar_sql(
        fecha,
        cotizaciones,
    )

    archivo_sql = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".sql",
            delete=False,
            encoding="latin-1",
            newline="\n",
        ) as archivo:

            archivo.write(
                contenido_sql
            )

            archivo_sql = Path(
                archivo.name
            )

        comandos = (
            f'CONNECT {ORACLE_USUARIO}/'
            f'"{ORACLE_PASSWORD}"@{ORACLE_ALIAS}\n'
            f'@{archivo_sql}\n'
        )

        resultado = subprocess.run(
            [
                str(SQLPLUS_EXE),
                "-L",
                "-S",
                "/nolog",
            ],
            input=comandos,
            capture_output=True,
            text=True,
            encoding="latin-1",
            errors="replace",
            timeout=60,
        )

        salida = resultado.stdout or ""
        salida_error = resultado.stderr or ""

        print("\nSalida de SQL*Plus:")
        print(salida)

        if salida_error:
            print(
                "\nSalida de error de SQL*Plus:"
            )
            print(
                salida_error
            )

        salida_completa = (
            salida
            + "\n"
            + salida_error
        ).upper()

        if resultado.returncode != 0:
            raise RuntimeError(
                "SQL*Plus terminó con código de error "
                f"{resultado.returncode}.\n\n"
                f"{salida}\n"
                f"{salida_error}"
            )

        if "ORA-" in salida_completa:
            raise RuntimeError(
                "Oracle devolvió un error:\n"
                f"{salida}\n"
                f"{salida_error}"
            )

        if "SP2-" in salida_completa:
            raise RuntimeError(
                "SQL*Plus devolvió un error:\n"
                f"{salida}\n"
                f"{salida_error}"
            )

        for esquema in ESQUEMAS_ORACLE:
            for codigo_moneda in cotizaciones:
                confirmacion = (
                    f"{esquema}_{codigo_moneda}_ACTUALIZADO".upper()
                )

                if confirmacion not in salida_completa:
                    raise RuntimeError(
                        "No se recibió confirmación de actualización "
                        f"para el esquema {esquema}, moneda "
                        f"{codigo_moneda}."
                    )

        if (
            "TODAS_LAS_COTIZACIONES_GUARDADAS"
            not in salida_completa
        ):
            raise RuntimeError(
                "No se pudo confirmar el COMMIT de las "
                "actualizaciones en Oracle."
            )

        detalle = ", ".join(
            f"{codigo_moneda}="
            + format(valor, ".4f").replace(".", ",")
            for codigo_moneda, valor in cotizaciones.items()
        )

        print(
            f"Cotizaciones guardadas para el {fecha:%d/%m/%Y} "
            f"({detalle}) en los esquemas: "
            + ", ".join(
                ESQUEMAS_ORACLE
            )
        )

    finally:
        if (
            archivo_sql is not None
            and archivo_sql.exists()
        ):
            archivo_sql.unlink()


# =============================================================================
# PROCESO PRINCIPAL
# =============================================================================

def main():
    global MODO_PRUEBA

    solo_consultar = "--solo-consultar" in sys.argv

    MODO_PRUEBA = solo_consultar

    if solo_consultar:
        print(
            "Modo --solo-consultar: no se escribe en Oracle "
            "ni se envían correos."
        )

    try:
        fecha, cotizaciones = (
            obtener_cotizacion_bna()
        )

        if solo_consultar:
            print(
                "\nModo --solo-consultar: no se escribió en Oracle."
            )
            return 0

        guardar_en_oracle(
            fecha,
            cotizaciones,
        )

        print(
            "\nProceso terminado correctamente."
        )

        return 0

    except requests.RequestException as error:
        print(
            "\nERROR consultando el BNA: "
            f"{error}"
        )

        enviar_mail_error(
            error
        )

        return 1

    except subprocess.TimeoutExpired as error:
        print(
            "\nERROR: SQL*Plus excedió el tiempo "
            "máximo de ejecución."
        )

        enviar_mail_error(
            error
        )

        return 1

    except Exception as error:
        print(
            f"\nERROR: "
            f"{type(error).__name__}: "
            f"{error}"
        )

        enviar_mail_error(
            error
        )

        return 1


def abrir_log():
    """
    Abre el archivo de log del mes.

    Si no se puede (carpeta inexistente, permisos, disco lleno),
    avisa por consola y devuelve None: el proceso igual debe poder
    actualizar la cotización aunque no se pueda loguear.
    """

    try:
        LOG_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        archivo_log = (
            LOG_DIR
            / f"cotizacion_{datetime.now():%Y-%m}.log"
        )

        return open(
            archivo_log,
            "a",
            encoding="utf-8",
        )

    except OSError as error:
        print(
            "AVISO: no se pudo abrir el archivo de log en "
            f"{LOG_DIR}: {error}"
        )
        print(
            "El proceso continúa, pero sin registro en archivo."
        )

        return None


if __name__ == "__main__":
    log = abrir_log()

    codigo_salida = 1

    try:
        if log is not None:
            sys.stdout = _Tee(sys.__stdout__, log)
            sys.stderr = _Tee(sys.__stderr__, log)

        print("=" * 70)
        print("Inicio de ejecución")

        if ARCHIVO_ENV.exists():
            print(f"Configuración leída de: {ARCHIVO_ENV}")
        else:
            print(
                f"AVISO: no existe {ARCHIVO_ENV}. "
                "Se usan los valores por defecto del script."
            )

        codigo_salida = main()

        print(
            "Fin de ejecución. Código de salida: "
            f"{codigo_salida}"
        )

    except BaseException:
        traceback.print_exc()
        raise

    finally:
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__

        if log is not None:
            log.close()

    sys.exit(
        codigo_salida
    )
