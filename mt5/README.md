# Notificaciones de MT5 a Telegram

`TradeNotifierTelegram.mq5` es un Expert Advisor que **no opera**: solo te avisa por Telegram cuando:

- se abre un trade (lado, lotes, símbolo, precio, SL/TP)
- se cierra un trade, total o parcial (ganancia o pérdida neta, precio de entrada y salida, motivo: TP, SL, manual…)
- cambias el SL o el TP de una posición
- hay un depósito o retiro

Detecta todos los trades de la cuenta, también los que abres desde el celular.

## 1. Crear el bot de Telegram

1. En Telegram, abre **@BotFather** y envía `/newbot`. Ponle un nombre y te da un **token** (algo como `123456789:AAH...`).
2. Abre tu bot nuevo y presiona **Iniciar** (`/start`). Si no lo haces, el bot no te puede escribir.
3. Abre **@userinfobot**. Te muestra tu **Id**: ese es tu **Chat ID**.

## 2. Instalar el EA en MT5 (PC)

1. En MT5: **Archivo → Abrir carpeta de datos → MQL5 → Experts**. Copia ahí `TradeNotifierTelegram.mq5`.
2. Abre el archivo con **MetaEditor** y presiona **Compilar** (F7). No debe mostrar errores.
3. **Herramientas → Opciones → Asesores Expertos**: marca **Permitir WebRequest para las siguientes URL** y agrega `https://api.telegram.org`.
4. En el Navegador, arrastra **TradeNotifierTelegram** a **un solo gráfico** (si lo pones en varios, te llegan mensajes repetidos).
5. En la pestaña **Parámetros de entrada**, pega el **Token** y el **Chat ID**. Presiona OK.
6. Activa el botón **Algo Trading** de la barra de herramientas.

Debe llegarte el mensaje "✅ Notificador conectado". Si no llega, revisa la pestaña **Expertos** (abajo, en la Caja de herramientas): ahí aparece el error.

## Importante

- El EA solo funciona mientras MT5 esté abierto en la PC. Para recibir avisos 24/7, usa un VPS (por ejemplo, el hosting virtual de MQL5: clic derecho en la cuenta → *Registrar un servidor virtual*).
- La app de MT5 en el celular no puede correr EAs.
- Si tu cuenta es de una empresa de fondeo (prop firm), revisa sus reglas sobre EAs. Este EA no envía órdenes.
