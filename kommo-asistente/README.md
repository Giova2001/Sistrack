# Kommo Asistente

Extensión de Chrome + chatbot local en Python (**sin IA**). Captura un mensaje de Kommo, sugiere una respuesta y te deja **copiar/pegar**.

## Requisitos

- Python 3.10+ con `fastapi` y `uvicorn` (ya están en el `requirements.txt` del repo)
- Google Chrome

## 1. Arrancar el servidor

En PowerShell:

```powershell
cd "kommo-asistente\server"
python -m uvicorn app:app --host 127.0.0.1 --port 8765 --reload
```

O doble clic en `kommo-asistente\server\run.bat`.

Prueba rápida: abre http://127.0.0.1:8765/ o http://127.0.0.1:8765/health  
(debe responder JSON `ok: true`; la API de sugerencias es `POST /api/suggest`)

## 2. Cargar la extensión

1. Chrome → `chrome://extensions`
2. Activa **Modo de desarrollador**
3. **Cargar descomprimida** → carpeta `kommo-asistente\extension`
4. Abre tu cuenta de Kommo

## 3. Uso

1. Entra a una conversación (chat WhatsApp en Kommo)
2. Pulsa el botón flotante **KA**
3. **Capturar** (o selecciona el texto del cliente y Capturar)
4. **Analizar**
5. **Copiar sugerencia** y pégala en el campo de Kommo

Si la captura automática no toma el mensaje correcto, selecciona el texto del cliente con el mouse y pulsa Capturar, o pégalo manualmente en el panel.

## Qué detecta el bot (reglas)

- Saludo / interés / rechazo
- Preguntas de precio, envío o pago
- Datos parciales o completos: nombre, WhatsApp, dirección
- Sugiere pedir solo lo que falta

Puedes editar las respuestas en `kommo-asistente\server\chatbot.py`.
