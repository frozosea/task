# main.py
import os
import asyncio
import uuid
import logging
from fastapi import FastAPI
from dotenv import load_dotenv

# Импорты для административного SDK
from livekit.api import LiveKitAPI, CreateRoomRequest, AccessToken, VideoGrants
from livekit import rtc
# Импортируем наш новый класс Bot
from echo_bot import main

load_dotenv()

URL = os.getenv('LIVEKIT_URL')
API_KEY = os.getenv('LIVEKIT_API_KEY')
API_SECRET = os.getenv('LIVEKIT_API_SECRET')
os.environ['LIVEKIT_URL'] = URL
os.environ['LIVEKIT_API_KEY'] = API_KEY
os.environ['LIVEKIT_API_SECRET'] = API_SECRET

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = FastAPI()

@app.get("/get-token")
async def get_token():
    room_name = f"echo-test-{uuid.uuid4()}"
    user_identity = f"user-{uuid.uuid4()}"
    bot_identity = f"bot-{uuid.uuid4()}"
    room = None 
    logging.info(f"--- New Test Session for Room: {room_name} ---")

    # 1. Создаем комнату через административный API
    async with LiveKitAPI() as lkapi:
        await lkapi.room.create_room(CreateRoomRequest(name=room_name))
        logging.info(f"Room '{room_name}' created via API.")
        room = rtc.Room(lkapi.room).connect()

    # 2. Создаем токен доступа для ФРОНТЕНДА, используя ИМЯ комнаты
    user_token = (
        AccessToken()
        .with_identity(user_identity)
        .with_grants(VideoGrants(room_join=True, room=room_name))
        .to_jwt()
    )
    
    # 3. Создаем экземпляр нашего Бота и запускаем его в фоне.
    #    Он сам подключится к созданной комнате.
    asyncio.create_task(main(room))

    # 4. Возвращаем URL и токен клиенту (фронтенду)
    return {"url": URL, "token": user_token}
