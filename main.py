import os
import asyncio
import logging
import json
import threading
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
import google.generativeai as genai
from mi_service_fix import MiAccount, MiHealth, XiaomiLoginError
from dotenv import load_dotenv

load_dotenv()

# Konfiguracja Gemini
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
model = genai.GenerativeModel('gemini-flash-latest')

# Serwer Flask dla platformy Render (aby zapobiec uśpieniu)
app = Flask(__name__)

@app.route('/')
def home():
    return "Serwer działa!", 200

# Logika pobierania danych z Xiaomi Mi Fitness
async def fetch_workout_data():
    account = None
    try:
        account = MiAccount(
            username=os.getenv("XIAOMI_USER"),
            password=os.getenv("XIAOMI_PASS"),
            token_path="./token.json"
        )
        health = MiHealth(account)

        logging.info("Próba pobrania listy treningów z Xiaomi...")
        data = await health.get_workout_list(limit=1)

        if not data or 'list' not in data or len(data['list']) == 0:
            logging.warning(f"Brak danych treningowych lub błąd: {data}")
            return None

        workout_id = data['list'][0]['workoutId']
        logging.info(f"Pobieranie szczegółów dla treningu ID: {workout_id}")
        detail = await health.get_workout_detail(workout_id)
        return detail

    except XiaomiLoginError:
        raise
    except Exception as e:
        logging.error(f"Błąd komunikacji z Xiaomi: {e}")
        return None
    finally:
        if account:
            await account.close()

# Obsługa komend Telegrama
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Cześć! Jestem Twoim asystentem sportowym AI.\n\n"
        "Co mogę zrobić:\n"
        "1. /analiza - Pobiorę Twój ostatni trening z Mi Fitness i go przeanalizuję.\n"
        "2. /status - Sprawdzę czy wszystko działa.\n"
        "3. Pisz do mnie śmiało - możemy porozmawiać o sporcie, diecie lub czymkolwiek chcesz!"
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_text = "📊 Status systemów:\n"
    
    try:
        model.generate_content("test")
        status_text += "✅ Gemini AI: Połączono\n"
    except Exception as e:
        status_text += f"❌ Gemini AI: Błąd ({e})\n"
        
    if os.getenv("XIAOMI_USER") and os.getenv("XIAOMI_PASS"):
        status_text += f"✅ Xiaomi: Dane logowania ({os.getenv('XIAOMI_USER')})\n"
    else:
        status_text += "❌ Xiaomi: Brak danych logowania w .env\n"
        
    await update.message.reply_text(status_text)

async def analiza(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = await update.message.reply_text("Pobieram pełne dane z Twojego konta Mi Fitness... Proszę o chwilę.")

    try:
        raw_data = await fetch_workout_data()
        if not raw_data:
            await status_msg.edit_text("Nie udało się pobrać danych (brak nowych treningów). Upewnij się, że Twoje treningi są zsynchronizowane w aplikacji Mi Fitness.")
            return
    except XiaomiLoginError as e:
        await status_msg.edit_text(f"❌ Błąd logowania Xiaomi:\n\n{e.description}")
        return
    except Exception as e:
        logging.error(f"Błąd podczas analizy: {e}")
        await status_msg.edit_text("Wystąpił nieoczekiwany błąd podczas pobierania danych.")
        return

    await status_msg.edit_text("Dane pobrane! Gemini AI rozpoczyna analizę Twojego treningu...")

    prompt = f"""
    Jesteś profesjonalnym trenerem sportowym. Przeanalizuj poniższe dane JSON z aplikacji Mi Fitness.
    
    Zadania:
    1. Zidentyfikuj sport i podaj kluczowe statystyki (czas, dystans, kalorie, tętno).
    2. Wyciągnij zaawansowane wnioski (np. tempo, kadencja, strefy tętna).
    3. Podaj 3 konkretne rady, jak poprawić ten wynik w przyszłości.

    Odpowiadaj po polsku, w sposób motywujący i techniczny.

    Dane treningu:
    {json.dumps(raw_data)[:8000]}
    """

    try:
        response = model.generate_content(prompt)
        await status_msg.edit_text(response.text)
    except Exception as e:
        logging.error(f"Błąd Gemini: {e}")
        await status_msg.edit_text("Wystąpił błąd podczas generowania analizy przez AI.")

# Obsługa zwykłej konwersacji
async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    if not user_text:
        return

    # Pokazujemy, że bot "pisze"
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        # Możemy dodać kontekst, że bot jest asystentem sportowym
        full_prompt = f"Jesteś asystentem sportowym AI. Użytkownik mówi: {user_text}"
        response = model.generate_content(full_prompt)
        await update.message.reply_text(response.text)
    except Exception as e:
        logging.error(f"Błąd Gemini w czacie: {e}")
        await update.message.reply_text("Przepraszam, mam chwilowy problem z myśleniem. Spróbuj za chwilę!")

# Funkcja uruchamiająca bota w tle
def run_bot():
    logging.info("Inicjalizacja bota Telegram...")
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        logging.error("Brak TELEGRAM_TOKEN w środowisku!")
        return

    try:
        application = ApplicationBuilder().token(token).build()
        
        # Handlery
        application.add_handler(CommandHandler('start', start))
        application.add_handler(CommandHandler('status', status))
        application.add_handler(CommandHandler('analiza', analiza))
        application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), chat))

        logging.info("Bot został uruchomiony i czeka na wiadomości...")
        application.run_polling()
    except Exception as e:
        logging.error(f"Krytyczny błąd bota: {e}")

if __name__ == '__main__':
    try:
        logging.basicConfig(
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            level=logging.INFO
        )

        logging.info("Start aplikacji...")

        # Uruchamiamy bota w tle, żeby nie blokował serwera
        bot_thread = threading.Thread(target=run_bot, daemon=True)
        bot_thread.start()

        # Uruchamiamy serwer Flask (wymagany przez platformę Render)
        port = int(os.environ.get("PORT", 10000))
        logging.info(f"Uruchamianie serwera Flask na porcie {port}...")
        
        # W nowszych wersjach Flask app.run() może próbować używać sygnałów
        # Na Renderze w wątku głównym powinno być OK.
        app.run(host='0.0.0.0', port=port)
    except Exception as e:
        logging.error(f"KRYTYCZNY BŁĄD URUCHAMIANIA: {e}", exc_info=True)
        os._exit(1)
