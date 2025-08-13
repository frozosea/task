🧭 Контекст
- Мы уже спроектировали Orchestrator (v3). Сейчас нам нужен медийный уровень: голосовой WebRTC-сервер на базе LiveKit, который:
- принимает входящий микрофон от клиента;
- локально делает VAD (голос/тишина) и barge-in (перебивание);
- стримит ответ бота обратно (8 кГц PCM -> Opus);
- общается с Оркестратором (пока мок-класс) через простой Python-интерфейс: отдаёт partial/final тексты и ивенты, получает аудио-ответ/команды.
- Цель демо: минимальные задержки, чёткая работа VAD/barge-in, лог-метрики. Глубокие тесты не нужны.

⸻

🎯 Что строим (кратко)
	1.	LiveKit Server локально (Docker).
	2.	Python-сервис webapi/voice_node:
- Подключается к LiveKit комнате как «бот» (publisher+subscriber).
- Забирает из комнаты входящее аудио пользователя.
- Ресемплит в PCM 8 кГц mono, режет на фреймы 20 мс.
- Гоняет через VAD Sierra (если не получится — drop-in через WebRTC-VAD как fallback).
- По VAD-ивентам:
- speech_started — фиксируем время начала фразы;
- speech_ended — выдаём final в мок-оркестратор;
- при активной отдаче бота, и если приходит новая активность — шлём barge_in_detected и останавливаем текущую отдачу.
- От Оркестратора берём аудио-ответ (для демо — эхо) и публикуем его в комнату как audio track (Opus), внешне это звучит как «бот отвечает».
- Ведём JSON-логи задержек: t1..t6 (см. ниже).
	3.	Простой клиент (web-страничка на LiveKit CDN): нажал Join → говоришь → видишь/слышишь ответ → проверяешь barge-in.

⸻

🧱 Дерево папок (минимум)

project_root/
├─ .env.example
├─ docker-compose.livekit.yml
├─ webapi/
│  ├─ __init__.py
│  ├─ voice_node/
│  │  ├─ __init__.py
│  │  ├─ config.py               # env + конфиги аудио/комнат/логов
│  │  ├─ livekit_runner.py       # соединение с LiveKit, join/subscribe/publish
│  │  ├─ audio_io.py             # ресемпл 48k->8k и обратно, фрейминг, PCM utils
│  │  ├─ vad.py                  # обёртка над Sierra VAD (+fallback WebRTC-VAD)
│  │  ├─ orchestrator_mock.py    # мок-оркестратор (эко, команды, события)
│  │  ├─ pipeline.py             # главная петля: приём → VAD → события → ответ
│  │  ├─ metrics.py              # метки t1..t6 + JSON-лог
│  │  └─ main.py                 # entrypoint (CLI: run-voice-node)
│  └─ static/
│     └─ client.html             # super-простой веб-клиент (Join/Leave)
└─ scripts/
   └─ gen_token.py               # jwt для LiveKit (локально, dev)


⸻

🔩 Зависимости
- Python: 3.10+ (ок, у нас 3.13, но можно 3.10).
- Pip пакеты:
- livekit-agents и/или livekit-rtc (клиент к LiveKit из Python)
- numpy, soundfile (libsndfile), pysoxr (или resampy)
- webrtcvad (fallback VAD). Там много проблем с доп пакетами, советую почитать доку на https://github.com/livekit/python-sdks 
- Sierra VAD: если есть pip-пакет — ставим; если нет, используем локальную библиотеку/модуль. В любом случае оборачиваем через наш vad.py с единым API.
- uvicorn, fastapi (если решим отдать клиентскую страничку из Python — удобно)
- python-dotenv, loguru (или structlog)
- LiveKit Server (docker образ), LiveKit CDN для client.html.

.env.example (заполнить и переименовать в .env):

# LiveKit
LIVEKIT_URL=ws://localhost:7880
LIVEKIT_API_KEY=devkey
LIVEKIT_API_SECRET=secret

# Room/Identity
LK_ROOM=demo
LK_BOT_IDENTITY=ingobot
LK_USER_PUBLISH_AUDIO=true

# Audio
AUDIO_TARGET_RATE=8000
AUDIO_FRAME_MS=20
AUDIO_CHANNELS=1

# Logging
LOG_FILE=voice_node.log
METRICS_FILE=metrics.jsonl


⸻

🐳 LiveKit локально

docker-compose.livekit.yml (уже готов), действия:
	1.	docker compose -f docker-compose.livekit.yml up -d
	2.	LiveKit UI: http://localhost:7880 (если включён) или просто используем URL из .env.
	3.	Токены для комнаты — генерим scripts/gen_token.py (JWT для client.html и для voice_node).

⸻

🧠 Аудио/потоки: как делаем
- Вход из браузера в LiveKit идёт как Opus 48 кГц.
- Мы на стороне Python подписываемся на трек, декодим в float32 48 кГц,
ресемплим → PCM int16 8 кГц, режем по 20 мс (160 сэмплов), mono.
- Этот 8к поток идёт:
- в VAD для событий speech_started/speech_ended;
- (по желанию) в STT (дальше подключим ваш stt_yandex).
- Ответ бота: получаем PCM 8 кГц int16 (от мок-оркестратора), при необходимости ресемплим в 48 кГц и публикуем как LiveKit audio track (Opus).
Для синхронизации «8к всё» — наш внутренний пайплайн 8 кГц. У клиента Opus → LiveKit сам подстроит.

⸻

📡 Протокол событий внутри узла

Сервис самодостаточен, но общается с (мок-)Оркестратором:

В сторону Оркестратора:
- on_speech_started(trace_id, ts_monotonic)
- on_partial_audio(trace_id, pcm8k_chunk) (оставим как задел — сейчас можно не использовать)
- on_speech_ended(trace_id, utterance_pcm8k, t_first_byte, t_last_byte) → оркестратор вернёт аудио-ответ
- on_barge_in_detected(trace_id) (когда пользователь перебивает во время BOT_TURN)

От Оркестратора:
- get_audio_reply(trace_id, user_pcm8k) → pcm8k_stream (генератор чанков)
- stop_playback(trace_id) (когда пришёл barge-in)
- play_filler(trace_id, key) (на будущее, сейчас можно no-op)

⸻

🧪 VAD и barge-in (правила)
- VAD окно: 20 мс фреймы, агрегация по скользящему буферу 200–300 мс.
- speech_started: ≥ N фреймов «voice» подряд (например, 6–8 фреймов).
- speech_ended: ≥ M фреймов «non-voice» подряд (например, 8–10 фреймов).
- barge-in:
- если идёт воспроизведение ответа бота и VAD уловил speech_started от пользователя →
	1.	немедленно вызвать orchestrator_mock.stop_playback(trace_id)
	2.	остановить публикацию текущего аудио-трека бота (или прервать буфер)
	3.	зафиксировать метрику barge_in: true и продолжить слушать пользователя.

Все пороги держим в config.py и выставляем простыми числами.

⸻

⏱️ Метрики/логи (минимум, JSON-строка на строку)

Файл: METRICS_FILE (по умолчанию metrics.jsonl)
- trace_id, turn_index
- t1_first_user_audio — приход первого байта записи фразы (после тишины)
- t2_speech_ended — зафиксирован конец речи (до STT)
- t3_logic_start — момент отправки в оркестратор (мок)
- t5_tts_first_chunk — получили первый аудио-чанк ответа
- t6_first_bot_audio — опубликовали первый чанк в LiveKit
- barge_in — true/false, playback_cancelled — true/false
- latency_user_to_first_bot_chunk_ms (t6 - t1)
- vad_speech_ms — длительность последней фразы по VAD

Логи сервиса (LOG_FILE): обычный JSON-лог с уровнем, сообщением, полями trace_id, room, и т. д.

⸻

🧪 Простой клиент (минимум)

webapi/static/client.html:
- Поле Token, Room, кнопка Join / Leave.
- При Join:
- подключаемся к LiveKit (через CDN скрипт),
- публикуем микрофон,
- подписываемся на аудио-трек бота.
- На экране можно выводить:
- индикатор VAD (получим из серверных toast через websockets? В демо можно пропустить),
- статус «barge-in случился» (опц. через логи).

(Если хочешь, отдай статику через FastAPI в main.py — 10 строк кода.)

⸻

🧪 Мок-Оркестратор (минимум, готов к вставке)

Это единственный код, чтобы не гонять тебя писать сам. Он простой и нужен для запуска демо.

# webapi/voice_node/orchestrator_mock.py
import asyncio
import time
from typing import AsyncIterator, Optional

class OrchestratorMock:
    """
    Очень простой мок:
    - Возвращает "эхо" текста/аудио пользователя через синтетику: sine-бип + паузы,
      либо проксирует заранее подготовленный PCM 8k (если подложим).
    - Умеет "останавливать" текущий стрим по barge-in (через флаг).
    - Логирует t4/t5 события через колбэки (выставляет их наружу).
    """
    def __init__(self):
        self._stop_flag = False
        self.on_tts_first_chunk = None  # optional: callable(trace_id, ts)
        self.on_llm_request_done = None # optional: callable(trace_id, ts)

    def stop_playback(self, trace_id: str):
        self._stop_flag = True

    async def get_audio_reply(self, trace_id: str, user_pcm8k: bytes) -> AsyncIterator[bytes]:
        """
        Генерирует 8к PCM монотоны. Для демо: сначала короткий сигнал, потом "эхо".
        Чанк ~20мс = 160 сэмплов = 320 байт (s16le mono).
        """
        # имитация "LLM готовит ответ"
        await asyncio.sleep(0.12)
        if self.on_llm_request_done:
            self.on_llm_request_done(trace_id, time.monotonic())

        frame = b"\x00\x00" * 160  # тишина 20мс
        beep = (b"\x7f\x00" * 80) + (b"\x00\x00" * 80)  # простой "бип" ~20мс

        # первый "бип" как признак tts first chunk
        if self.on_tts_first_chunk:
            self.on_tts_first_chunk(trace_id, time.monotonic())
        yield beep

        # затем 40 фреймов "тихой речи"
        for _ in range(40):
            if self._stop_flag:
                self._stop_flag = False
                return
            await asyncio.sleep(0.02)
            yield frame

В реальной связке сюда подставится TTSManager.start_llm_stream() и настоящий ответ. Для демо достаточно.

⸻

🧩 Поведение сервиса (end-to-end)
	1.	voice_node заходит в комнату LK_ROOM как LK_BOT_IDENTITY, подписывается на первый микрофон пользователя.
	2.	Идёт непрерывная обработка:
- ресемпл в 8 кГц, фрейминг по 20 мс, VAD;
- speech_started → отметка t1;
- speech_ended → собираем фразу (буфер PCM 8k), фиксируем t2, вызываем orchestrator_mock.get_audio_reply(), фиксируем t3 (в момент вызова) и t5 (колбэк первого чанка),
- публикуем аудио как LiveKit track → t6;
	3.	Если во время отдачи бота VAD ловит голос пользователя → barge-in:
- вызывать orchestrator_mock.stop_playback(),
- останавливать публикацию трека,
- логировать barge_in=true, playback_cancelled=true,
- начинать слушать пользователя.

⸻

⚙️ Как запускать (локально)
	1.	LiveKit:

docker compose -f docker-compose.livekit.yml up -d


	2.	Токен для клиента и бота:

python scripts/gen_token.py --room demo --identity ingobot --api-key devkey --api-secret secret
# для браузерного клиента возьми отдельный identity, например "me"


	3.	Сервис:

pip install -U livekit-agents livekit-rtc numpy soundfile pysoxr webrtcvad loguru python-dotenv fastapi uvicorn
export $(cat .env | xargs)  # или положи .env рядом
python -m webapi.voice_node.main --room demo --identity ingobot


	4.	Клиент:
- открой webapi/static/client.html в браузере;
- вставь токен, нажми Join;
- говори в микрофон → услышь эхо бота;
- начни говорить, пока бот «говорит» → должен сработать barge-in (бот мгновенно заткнётся).

⸻

🗂️ Задачи (малые, последовательные)

Задача 1. Инфра и конфиги

Цель: поднять LiveKit локально, токены, .env, заготовки файлов.
- Добавить docker-compose.livekit.yml, .env.example, scripts/gen_token.py.
- Наполнить webapi/voice_node/config.py (чтение env, пороги VAD, размеры фреймов).
- Добавить webapi/static/client.html (кнопка Join/Leave, ввод токена, подключение микрофона, подписка на remote track).
- Проверка: клиент заходит в комнату, видит себя в списке участников.

Готовность: можно подключаться к комнате через браузер.

⸻

Задача 2. Подключение Python к LiveKit

Цель: бот в комнате, подписывается на входящий аудио-трек пользователя, публикует свой трек.
- Реализовать livekit_runner.py: join room, subscribe на первый audio track, publish audio track «bot_out».
- Протокол колбэков: on_track_subscribed(raw frames 48k), publisher.sink(generator of 48k frames).
- В main.py — CLI, graceful shutdown, логирование.

Готовность: бот принимает исходящее аудио и умеет отдавать свой сигнал (например, синус).

⸻

Задача 3. Аудио-пайплайн (ресемпл и фрейминг)

Цель: внутри узла всё гоняем в PCM 8 кГц mono, 20 мс.
- audio_io.py:
- resample_48k_to_8k(float32->int16), resample_8k_to_48k(int16->float32)
- frame_8k_20ms(pcm8k_bytes) -> Iterable[bytes], join_frames(frames)->bytes.
- Микротест: подать синус 48k, получить 8k, собрать/разобрать по 20 мс.

Готовность: стабильный конвертер с точными размерами: 20 мс → 160 сэмплов → 320 байт.

⸻

Задача 4. VAD (Sierra + fallback) и события

Цель: чёткая детекция начала/конца речи + barge-in.
- vad.py с единым классом VoiceActivityDetector:
- backend "sierra" (если библиотека доступна) и "webrtc" как fallback;
- методы: is_speech(frame_20ms_8k_bytes) -> bool.
- В pipeline.py: буфер состояний, пороги (настраиваемые через config.py):
- speech_started: N голосовых подряд (6–8);
- speech_ended: M тишин подряд (8–10).
- Генерить события в мок-оркестратор: on_speech_started, on_speech_ended(...).

Готовность: в логах видно begin/end речи с временными метками.

⸻

Задача 5. Barge-in

Цель: во время проигрывания ответа бота — прерывать по новой речи пользователя.
- В pipeline.py хранить флаг bot_playing и handler активной публикации.
- При speech_started и bot_playing==True:
- вызвать orchestrator_mock.stop_playback(trace_id);
- остановить publish («bot_out»);
- логировать barge_in=true, playback_cancelled=true.

Готовность: руками проверяем: начинаем говорить — бот мгновенно затыкается.

⸻

Задача 6. Мок-Оркестратор и поток ответа

Цель: связать конец речи пользователя с получением аудио-ответа (эхо).
- Подключить orchestrator_mock.py в pipeline.py.
- На on_speech_ended:
- фиксируем t2;
- вызываем get_audio_reply (фиксируем t3 в момент вызова);
- на первом выходном чанке — колбэк on_tts_first_chunk → t5;
- публикуем поток в LiveKit → первый опубликованный чанк = t6.
- Метрики/логи писать в metrics.jsonl.

Готовность: слышим короткий «бип» и тихую дорожку «эха».

⸻

Задача 7. Клиент и smoke-проверки

Цель: простая браузерная проверка живьём.

- Открыть client.html, вставить токен, Join.
- Произнеси «Раз-два-три» → бот ответил; начни говорить в момент ответа → barge-in.
- Проверить файл metrics.jsonl: поля t1..t6, latency.

Готовность: демо «говорю/перебиваю/слышу» стабильно работает.

⸻

📏 Что мерим (минимум)
- latency_user_to_first_bot_chunk_ms = t6 - t1 — целевая < 800 мс (оценка по демо).
- vad_speech_ms — длительность речи по VAD.
- Наличие и частота barge_in.
- В логах видеть: start/stop playback, drop по barge-in, номера фреймов, размеры буферов.

⸻

🔌 Параметры и тонкая настройка

config.py (предложение по дефолтам):

- AUDIO_TARGET_RATE = 8000
- AUDIO_FRAME_MS = 20
- VAD_BACKEND = "sierra" (fallback "webrtc")
- VAD_SPEECH_START_FRAMES = 6
- VAD_SPEECH_END_FRAMES = 10
- PUBLISH_TRACK_NAME = "bot_out"
- ROOM = os.environ["LK_ROOM"], IDENTITY = os.environ["LK_BOT_IDENTITY"]

Все значения — переключаемые через env.

⸻

🧯 Ограничения и оговорки (нормально для демо)
- В браузере LiveKit всегда отдаёт Opus 48 кГц. Мы внутри держим 8 кГц PCM, а при публикации назад LiveKit перекодирует в Opus (48 кГц). Это ок для демо и синхронизации с будущими STT/TTS.
- Sierra VAD: если не взлетит «из коробки», обязательно оставить webrtcvad fallback (он быстрый и достаточный).
- Никаких сложных перезапусков/репликаций: одна комната, один бот.
- Без глубоких автотестов. Нам важны ручные smoke + JSON-метрики.

⸻

✅ Критерии готовности демо
- Клиент подключается, говорит — слышит ответ от бота.
- При разговоре поверх ответа → бот затыкается (barge-in).
- В metrics.jsonl корректно пишутся t1..t6 и сводные задержки.
- В voice_node.log видны ключевые события (join, subscribe, publish, speech_started/ended, barge_in).

⸻

если в процессе окажется, что Sierra VAD недоступен в виде готового питон-пакета — не останавливаемся: включаем webrtcvad как основной, а Sierra подвезём позже тем же интерфейсом VoiceActivityDetector.
