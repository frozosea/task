### **Спецификация оркестратора для референса**

#### **1. Контекст и цель модуля (CONTEXT)**

`Orchestrator` — это **«дирижёр»** жизненного цикла одного звонка. Он не содержит бизнес‑логики (не знает, как считать стоимость) и не разбирает речь (не знает, как работают эмбеддинги). Его единственная задача — управлять **полным жизненным циклом одного звонка**, координируя асинхронное взаимодействие между всеми остальными модулями для обеспечения плавного, отказоустойчивого и измеряемого диалога.

- **Жизненный цикл:** для **каждого нового звонка** создаётся **новый, изолированный экземпляр** `Orchestrator`. Он «живёт» ровно столько, сколько длится звонок, и уничтожается после его завершения, гарантируя отсутствие утечек состояния между звонками.

---

#### **2. Архитектура и ключевые обязанности**

`Orchestrator` — это асинхронный класс, реализующий главный цикл обработки диалога и управляющий всеми исключительными ситуациями.

##### **2.1. Управление состоянием **

- `Orchestrator` является **владельцем** объекта `SessionState`.
- `SessionState` — это строго типизированный `dataclass`. Это обеспечивает валидацию данных, автодополнение в IDE и предотвращает ошибки из‑за опечаток в ключах. Он содержит `call_id`, `current_state_id`, словарь `variables`, историю состояний `state_history` и `previous_intent_leader` для проверки стабильности.

##### **2.2. Маршрутизация решений и обработка фолбэков**

- Реализует основной `try…except` блок для отлова падений внешних сервисов (`STT`, `TTS`, `LLM`). В случае ошибки инициирует проигрывание «аварийного» филлера.
- Маршрутизирует запросы между `FlowEngine` (для сценариев) и `LLMManager` (для возражений и вопросов «не по теме»).
- Проверяет ответ от `FlowEngine`/`LLMManager` на наличие флага `end_call: true` и инициирует завершение звонка.

##### **2.3. Управление аудио‑плейлистом и филлерами**

- Перед вызовом потенциально долгой операции (`LLMManager` или `TTSManager`) `Orchestrator` проверяет `dialogue_map.json` на наличие мета‑поля `fillers: List[str]` для текущего состояния.
- Если филлеры указаны, он **проактивно** начинает их стриминг, **параллельно** запуская долгую операцию в фоновой задаче.
- **Фолбэк:** если основная задача не завершилась после проигрывания всех указанных филлеров, `Orchestrator` начинает в цикле проигрывать «нейтральный» филлер (`filler:still_working`) до глобального таймаута операции.

##### **2.4. Умный проактивный вызов LLM**

- Получив `partial` результат от `STT`, `Orchestrator` делает быстрый вызов `IntentClassifier`.
- Когда приходит `final` результат от `STT`, `Orchestrator` делает финальную классификацию. Если она неуспешна, то мы вызываем генерацию LLM на основе запроса юзера. 

##### **2.5. Обработка перебиваний (Barge‑in)**

- `Orchestrator` поддерживает внутреннее состояние: `BOT_TURN` или `USER_TURN`.
- Orchestrator хранит ссылку на текущую задачу проигрывания аудио в атрибуте self.current_playback_task.
- Сигнал `barge_in_detected` от `TelephonyGateway` во время `BOT_TURN` немедленно отменяет немедленно вызывает новый метод handle_barge_in(), который:
  - Проверяет, существует ли self.current_playback_task и активна ли она. 
  - Вызывает self.current_playback_task.cancel() для немедленной остановки аудио-потока. 
  - Переключает состояние self.session_state.turn_state в USER_TURN.

##### **2.6. Сбор и логирование метрик**

- `Orchestrator` использует специальный класс‑помощник `MetricsLogger`, который инициализируется с уникальным `trace_id` для каждого звонка.
- Он фиксирует не только временные метки (`t1`…`t6`), но и **контекстные атрибуты** для каждой операции (например, `model_name`, `confidence_score`), обеспечивая полноценную трассировку.

---

#### **3. Структуры данных (**``******************************************)**

```python
# domain/models.py

from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Literal
import time

# --- Типы для статусов ---
FlowStatus = Literal['SUCCESS', 'EXECUTION_ERROR', 'MISSING_ENTITY']

# --- Объекты для IntentClassifier ---
@dataclass(slots=True)
class IntentResult:
    """Результат успешной классификации намерения по сценарию."""
    intent_id: str
    score: float
    entities: Optional[Dict[str, Any]]
    current_leader: str  # ID интента‑лидера для этого шага

# --- Объекты для FlowEngine ---
@dataclass(slots=True)
class FlowResult:
    """Результат обработки события движком сценария."""
    status: FlowStatus
    updated_session: Optional[dict] = None  # Возвращается только при статусе SUCCESS

# --- Объекты для Orchestrator ---
@dataclass
class SessionState:
    """
    Полное состояние одного звонка.
    Этот объект — "единый источник правды" для Оркестратора.
    """
    call_id: str
    current_state_id: str = "start"
    variables: Dict[str, Any] = field(default_factory=dict)
    state_history: List[str] = field(default_factory=list)  # Для логики возврата
    previous_intent_leader: Optional[str] = None
    turn_state: Literal['BOT_TURN', 'USER_TURN'] = 'BOT_TURN'

@dataclass
class MetricsLog:
    """Структура для сбора и логирования метрик производительности."""
    trace_id: str
    turn_index: int = 0
    # t1: Время получения первого аудио‑байта от клиента
    t1_first_user_audio: Optional[float] = None
    # t2: Время получения финального текста от Yandex STT
    t2_stt_final: Optional[float] = None
    # t3: Время отправки запроса в IntentClassifier/FlowEngine
    t3_logic_start: Optional[float] = None
    # t4: Время отправки запроса в OpenAI LLM (если был)
    t4_llm_request: Optional[float] = None
    # t5: Время получения первого аудио‑чанка от TTS
    t5_tts_first_chunk: Optional[float] = None
    # t6: Время отправки первого аудио‑чанка клиенту
    t6_first_bot_audio: Optional[float] = None

    def start_turn(self):
        self.turn_index += 1
        self.t1_first_user_audio = time.monotonic()
        # Сброс остальных таймеров
        self.t2_stt_final = (
            self.t3_logic_start
        ) = (
            self.t4_llm_request
        ) = (
            self.t5_tts_first_chunk
        ) = self.t6_first_bot_audio = None
```

---

### 4. Детальная спецификация класса `Orchestrator`

#### 4.1. Init
    Class Orchestrator:
        def __init__(
            self,
            call_id: str,
            flow_engine: AbstractFlowEngine,
            intent_classifier: AbstractIntentClassifier,
            tts_manager: AbstractTTSManager,
            tts_websocker_connnection_manager: WebSocketConnectionManager
            neutral_fillers_keys: List[str], #ключи для редиса нейтральный филеров чтобы забить флоу пока нет ответа
            non_secure_response: str,
            # ... и так далее для всех зависимостей
        ):
            self.flow_engine = flow_engine
            self.intent_classifier = intent_classifier
            self.tts_websocker_connnection_manager = tts_websocker_connnection_manager
            # Атрибут для хранения задачи, которую можно будет отменить
            self.current_playback_task: Optional[asyncio.Task] = None
            async def _setup_connections(self):
                await self.tts_connection_manager.connect()
                # ... другие асинхронные настройки
    
    # "Фабрика" для создания и настройки экземпляра
    async def create_orchestrator_instance(call_id, ...) -> Orchestrator:
        # ... создаем все зависимости ...
        tts_conn = WebSocketConnectionManager(...)
    
        orchestrator = Orchestrator(call_id, ..., tts_connection_manager=tts_conn)
        await orchestrator._setup_connections()  # <-- Асинхронный вызов здесь
    
        # Если соединение не удалось, _setup_connections выбросит исключение,
        # и мы можем его здесь поймать и завершить звонок.

         return orchestrator
        # ..

**Назначение:** Конструктор.

**Логика:**

- Принимает `call_id` и словарь `dependencies` со всеми Singleton‑сервисами.
- Инициализирует `self.session_state = SessionState(call_id=call_id)`.
- Инициализирует `self.metrics_logger = MetricsLogger(trace_id=call_id)`.
- Инициализирует атрибут self.current_playback_task = None
- Инициализирует per‑call модули: `self.llm_manager = LLMManager(...)`.

#### 4.2. `async run(self, inbound_stream, outbound_stream)`

**Назначение:** Главный метод, запускающий основной цикл диалога.

**Логика:**

1. Инициализирует `self.stt_manager` с `inbound_stream`.
   1. Создать YandexSTTStreamer(). 
   2. Создать audio_chunk_queue = asyncio.Queue(maxsize=...). 
   3. Вызвать response_queue = stt_streamer.start_recognition(audio_chunk_queue). 
   4. Запустить фоновую задачу, которая будет читать байты из inbound_stream и класть их в audio_chunk_queue. 
   5. Запустить главную задачу-слушателя, которая будет читать результаты из response_queue (включая barge_in).
2. Запускает приветствие бота (первый ход).
   1. Устанавливает self.session_state.turn_state = 'BOT_TURN'. 
   2. Получает конфигурацию для начального состояния из self.dialogue_map[self.session_state.current_state_id]. 
   3. Извлекает playlist_config = initial_state['system_response']['playlist']. 
   4. Вызывает await self._play_audio_playlist(playlist_config, outbound_stream), чтобы произнести приветстви
4. Внутри цикла вызывает `await self._dialogue_loop(stt_response_queue, outbound_stream)`.
5. Использует `try…finally` для гарантированного вызова `self.shutdown()` в конце.

#### 4.3. `_handle_unscripted_flow`
Логика:
1. Вызвать `text_input_queue, audio_output_queue = await self.tts_manager.start_llm_stream()`. Если мы видим что упали с ошибкой, то нужно проиграть филер дернув его из кэша self.non_secure_response. Класс ошибки при ошибки подключения будет TTSConnectionError
2. Вызвать `llm_text_stream = self.llm_manager.process_user_turn(...)`.
3. Запустить две фоновые задачи:
    * **Задача 1 (Pipe):** `asyncio.create_task(self._pipe_llm_to_tts(llm_text_stream, text_input_queue))`. Она должна итерироваться по llm_text_stream, из каждого чанка LLMStreamChunk извлекать поле text_chunk и уже его класть в очередь для TTS.
    * **Задача 2 (Playback):** `asyncio.create_task(self._stream_audio_from_queue(audio_output_queue, outbound_stream))`. Эта корутина читает аудио из очереди TTS и стримит его пользователю. Мы должны сохранить айди задачи, чтобы в случае перебивания отменить ее через asyncio
    * **Задача 3 (Saving):** `self.current_playback_task = playback_task`
4. Как исправить: Orchestrator должен обработать первый чанк из llm_text_stream до того, как запускать задачи Pipe и Playback. 
   1. Получить первый чанк из потока LLM. 
   2. Проверить метаданные (is_safe). 
   3. Если is_safe is False: отменить дальнейшую обработку через `await LLMManager.abort_generation()`, проиграть non_secure_response и повторить запрос. 
   4. Если is_safe is True: передать этот первый чанк (его текстовую часть) в очередь TTS, а затем запустить задачи Pipe и Playback для обработки остального потока.
5. Зафиксировать `t4` метрику по окончанию 

#### 4.4. `async _dialogue_loop(self, stt_response_queue, outbound_stream)`

**Назначение:** Приватный метод для одного полного цикла «вопрос‑ответ».

**Логика (пошагово):**

2. **Вход в бесконечный цикл:** while not self.call_ended:. 
3. Устанавливает self.session_state.turn_state = 'USER_TURN'
4. **Ожидание речи:** Асинхронно ожидает новое сообщение из очереди: stt_result = await stt_response_queue.get()`. Фиксирует метрики `t1` и `t2`.
5. **Обработка `partial` результатов**
   1. Если stt_result.is_final is False
      1. Фиксирует `t3`
      2. Вызывает intent_result = await self.intent_classifier.classify_intent(...) с partial текстом.
      3. Сохраняет previous_leader в SessionState
      4. Продолжает цикл, не дожидаясь final результата (continue).
6. **Обработка `final` результатов**:
   1. Фиксирует метрики t1 и t2
   2. Финальная классификация: Вызывает intent_result = await self.intent_classifier.classify_intent(...) с final текстом.
   3. **Маршрутизация:**
      - Если `intent_result` есть (сценарий):
      - Проверяет, требует ли интент сущности и получены ли они. Если нет, формирует плейлист для уточняющего вопроса и завершает ход.
      - Вызывает `flow_result = self.flow_engine.process_event(...)`.
      - Если `flow_result.status == 'SUCCESS'`, обновляет `self.session_state` и формирует плейлист для ответа на основе нового `current_state_id`.
      - Если `flow_result.status != 'SUCCESS'`, формирует плейлист для сообщения об ошибке.
      - Если `intent_result` нет (не по сценарию) - вызываем сначала пробуем поиск по базе знаний, через: `await self.intent_classifier.find_faq_answer(text: str)` , else: `self.handle_unscripted_flow`:
7. **Генерация и стриминг аудио:** 
   1. Устанавливает self.session_state.turn_state = 'BOT_TURN'
   2. Вызывает `await self._play_audio_playlist(playlist, outbound_stream)`, который управляет филлерами, кэшем, TTS и стримингом. 
   3. Фиксирует `t5` и `t6`.
8. **Обновление состояния:** обновляет `self.session_state.previous_intent_leader = intent_result.current_leader` для следующего хода.

**Когда мы завершаем звонок?:**
- Получает от IntentClassifier результат, например, intent_rejection_hangup.
- Передает его в FlowEngine. FlowEngine видит в transitions этого интента, что next_state — это state_goodbye_and_hangup, и обновляет SessionState.
- Orchestrator получает обновленное состояние.
- Он лезет в dialogue_map.json, чтобы понять, что делать дальше для состояния state_goodbye_and_hangup.
- Он строит плейлист для аудио (static:polite_goodbye).
- И самое главное: он проверяет, есть ли в этом состоянии поле "action". Он видит "action": "END_CALL" и понимает: "Ага, после того как я проиграю этот плейлист, я должен завершить звонок".
- Orchestrator вызывает await self._play_audio_playlist(...).
- Сразу после этого он выставляет свой внутренний флаг self.call_ended = True.
- Основной цикл run() завершается, и вызывается self.shutdown().



#### 4.5. `async _play_audio_playlist(self, playlist_config, outbound_stream)`

**Назначение:** Умный проигрыватель аудио‑ответов.

**Логика:**

- Получает `playlist_config` из `dialogue_map.json` (список ключей для кэша, текста для TTS и филлеров).
- Если в плейлисте есть долгая TTS или LLM операция, запускает её в фоновой задаче (`asyncio.create_task`).
- Одновременно начинает стримить в `outbound_stream` филлеры из кэша. Которые приходят в формате: 
  - ```json
    {
    "playlist": [
    {
        "type": "cache",
        "key": "static:final_price_intro"
    },
    {
       "type": "filler",
       "key": "filler:hmm"
    },
    {
       "type": "tts",
       "text_template": "{{ session.variables.final_price }}"
    },
    {
       "type": "cache",
       "key": "static:currency_rubles"
    }
                ]
    }
    ```
- После завершения филлеров ожидает и начинает стримить результат фоновой задачи.
- Сохранение задачи: Все задачи, отвечающие за проигрывание аудио в outbound_stream (будь то стриминг из кэша, филлеров или результата TTS), должны быть объединены в одну родительскую задачу через asyncio.gather или asyncio.create_task. Ссылка на эту родительскую задачу сохраняется: self.current_playback_task = playback_parent_task.
- В случае если мы отдали последний чанк филера при этом не получили результат от llm+tts, тогда мы проигрываем нейтральный филер из self.neutral_fillers_keys. Нужно строго считать, что если мы начали стриминг нейтрального филера и при этом резко получили ответ, тогда мы резко стопим стриминг филера и начинаем стриминг уже реального ответа.  Она требует очень аккуратного использования asyncio.wait() с флагом FIRST_COMPLETED и чёткой логики отмены задач. Ошибка в этой логике может привести либо к неприятным обрывам звука, либо к тому, что нейтральный филлер проиграется целиком, хотя основной ответ уже был готов, что увеличит задержку. Филеры проигрываем последовательно. 

#### 4.5. `async shutdown(self)`

Назначение: Корректное завершение работы.

Логика:
- Вызывает self.llm_manager.shutdown().
- Вызывает self.stt_manager.stop_recognition().
- Отправляет финальный лог с метриками.

