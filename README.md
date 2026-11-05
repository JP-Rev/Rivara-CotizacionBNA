# Cotización BNA → Oracle

Script en Python que lee la cotización del dólar publicada por el
**Banco de la Nación Argentina** (`www.bna.com.ar/Personas`) y la graba en
la tabla `MONEDAS_COTIZACIONES` del ERP, en uno o varios esquemas Oracle.

Reemplaza a una versión anterior que consultaba la API de Errepar.

## Qué hace

1. Descarga la página de cotizaciones del BNA (con reintentos).
2. Lee la solapa **Divisas** o **Billetes** y extrae la fila del dólar
   (compra y venta) junto con la fecha publicada.
3. Valida el dato antes de tocar la base.
4. Arma un `MERGE` por cada esquema y moneda, y lo ejecuta con **SQL\*Plus**
   dentro de una única transacción.
5. Deja registro en un log mensual y, ante un error, envía un correo.

## Requisitos

- Python 3.8 o superior
- [`requests`](https://pypi.org/project/requests/) (único paquete externo)
- Cliente Oracle con **SQL\*Plus** (solo para escribir en la base)

```bash
pip install requests
```

El parser de HTML y el lector de `.env` usan únicamente la biblioteca
estándar: no hace falta `beautifulsoup4` ni `python-dotenv`.

## Instalación

```bash
git clone https://github.com/JP-Rev/Rivara-CotizacionBNA.git
cd Rivara-CotizacionBNA
copy .env.example .env
```

Editá el `.env` con los datos de tu entorno. **El `.env` no se versiona**
(está en `.gitignore`): contiene las claves de Oracle y de SMTP.

## Uso

Prueba en seco — consulta el BNA, muestra lo que haría y **no escribe en
Oracle ni envía correos**:

```bash
python cotizacion_bna.py --solo-consultar
```

Ejecución normal:

```bash
python cotizacion_bna.py
```

Devuelve código de salida `0` si todo salió bien y `1` ante cualquier
error, de modo que el Programador de tareas de Windows pueda detectarlo.

## Configuración

Todo se controla desde el `.env`. Ver `.env.example` para la lista
completa. Los más relevantes:

| Variable | Descripción |
|---|---|
| `ORACLE_USUARIO` / `ORACLE_PASSWORD` / `ORACLE_ALIAS` | Conexión a la base |
| `ESQUEMAS_ORACLE` | Esquemas a actualizar, separados por coma |
| `SQLPLUS_EXE` | Ruta del ejecutable de SQL\*Plus |
| `TIPO_COTIZACION` | `divisas` (default) o `billetes` |
| `MONEDAS_A_ACTUALIZAR` | Mapeo código → valor. Ej: `D=venta,C=compra` |
| `EXIGIR_FECHA_ACTUAL` | `true` aborta si el BNA aún no publicó la de hoy |

Prioridad de los valores: variables de entorno del sistema → `.env` →
valores por defecto del script.

### Mapeo de monedas

La tabla `MONEDAS_COTIZACIONES` guarda **un solo valor por moneda y
fecha** (`MCZ_COTIZACION`, `NUMBER(11,4)`). La compra y la venta se
distinguen por el código en `MCZ_MONEDA`:

| `MCZ_MONEDA` | Descripción en `MONEDAS` | Valor del BNA |
|---|---|---|
| `D` | DOLARES | venta |
| `C` | Dolar comprador | compra |

Eso es lo que expresa `MONEDAS_A_ACTUALIZAR=D=venta,C=compra`. Para
actualizar solo la venta, dejá `MONEDAS_A_ACTUALIZAR=D=venta`.

## Validaciones

El proceso aborta sin escribir nada si:

- el BNA no publicó la cotización del día (con `EXIGIR_FECHA_ACTUAL=true`);
- la fecha publicada es futura;
- la venta es menor que la compra (indicio de columnas invertidas);
- algún valor es cero, negativo o excede `NUMBER(11,4)`;
- SQL\*Plus devuelve `ORA-` o `SP2-`, o no confirma todos los esquemas.

Los `MERGE` van dentro de una sola transacción: si falla uno, no se
confirma ninguno.

## Programación

El BNA actualiza la pizarra durante la mañana. Si la tarea corre muy
temprano, la página todavía muestra la cotización del día anterior y el
proceso aborta (comportamiento esperado con `EXIGIR_FECHA_ACTUAL=true`).
Conviene programarla cerca del mediodía, o bien poner
`EXIGIR_FECHA_ACTUAL=false` para aceptar la última publicada.

## Logs

Se escribe un archivo por mes en `LOG_DIR` (por defecto, la subcarpeta
`Logs` junto al script), con fecha y hora en cada línea. Si no se puede
abrir el archivo, el proceso continúa igual y solo avisa por consola.
