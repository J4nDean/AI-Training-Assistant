import os
import httpx
import logging
from fastapi import FastAPI, Request, HTTPException
from google import genai
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = FastAPI()

# Configuration
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "supersecret_default")

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    logger.error("Missing environment variables: TELEGRAM_TOKEN or GEMINI_API_KEY")

# Initialize Gemini Client
client = genai.Client(api_key=GEMINI_API_KEY)

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

SYSTEM_PROMPT = """
Jesteś pomocnym asystentem treningowym. 
Odpowiadaj po polsku, krótko i konkretnie. 
Jeśli pytanie dotyczy treningu, dawaj praktyczne wskazówki.
"""

@app.get("/")
async def root():
    return {"status": "ok", "bot": "active"}

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    # Security: check secret token from Telegram
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret != WEBHOOK_SECRET:
        logger.warning("Invalid secret token received")
        raise HTTPException(status_code=403, detail="Invalid secret")

    try:
        data = await request.json()
    except Exception:
        return {"ok": False, "error": "Invalid JSON"}

    message = data.get("message")
    if not message:
        return {"ok": True}

    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    if not text:
        await send_message(chat_id, "Na razie obsługuję tylko wiadomości tekstowe.")
        return {"ok": True}

    if text == "/start":
        await send_message(chat_id, "Cześć! Jestem Twoim asystentem treningowym AI. Napisz mi o swoim treningu!")
        return {"ok": True}

    # Prepare prompt for Gemini
    prompt = f"{SYSTEM_PROMPT}\n\nUżytkownik: {text}"

    try:
        # Use Gemini 2.0 Flash
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt
        )
        answer = response.text if response.text else "Nie udało mi się przygotować odpowiedzi."
    except Exception as e:
        logger.error(f"Gemini API error: {e}")
        answer = "Wystąpił błąd podczas komunikacji z AI."

    await send_message(chat_id, answer[:4000])
    return {"ok": True}

async def send_message(chat_id: int, text: str):
    url = f"{TELEGRAM_API}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text
    }
    async with httpx.AsyncClient() as http_client:
        try:
            await http_client.post(url, json=payload)
        except Exception as e:
            logger.error(f"Telegram send error: {e}")

@app.post("/set-webhook")
async def set_webhook():
    # RENDER_EXTERNAL_URL is automatically provided by Render
    webhook_url = os.getenv("RENDER_EXTERNAL_URL")
    if not webhook_url:
        raise HTTPException(status_code=500, detail="Missing RENDER_EXTERNAL_URL")

    url = f"{TELEGRAM_API}/setWebhook"
    payload = {
        "url": f"{webhook_url}/telegram/webhook",
        "secret_token": WEBHOOK_SECRET
    }

    async with httpx.AsyncClient() as http_client:
        resp = await http_client.post(url, json=payload)
        return resp.json()
