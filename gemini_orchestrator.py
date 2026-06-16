import sqlite3
import asyncio
import time
import random
import logging
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
import google.generativeai as genai
from google.api_core import exceptions

# Configure logging
logger = logging.getLogger("GeminiOrchestrator")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

class AllModelsExhaustedError(Exception):
    """Raised when all Gemini models have exhausted their rate limits (RPD/RPM)."""
    pass

class GeminiStateManager:
    """
    Manages state for the Gemini Orchestrator using a hybrid approach:
    - RPD (Requests Per Day) is stored in SQLite to survive bot restarts.
    - RPM (Requests Per Minute) and spacing intervals are tracked in RAM to avoid disk I/O.
    """
    def __init__(self, db_path: str = "gemini_orchestrator_state.db"):
        self.db_path = db_path
        self.db_lock = asyncio.Lock()
        
        # RAM state for RPM and spacing: model_name -> deque of timestamps
        self.rpm_logs: Dict[str, deque] = {}
        # RAM state for last request timestamp: model_name -> float
        self.last_request_time: Dict[str, float] = {}

    def _init_db(self) -> None:
        """Initializes database schema. Runs in worker thread."""
        conn = sqlite3.connect(self.db_path, timeout=20.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_usage (
                    model_name TEXT,
                    usage_date TEXT,
                    request_count INTEGER,
                    PRIMARY KEY (model_name, usage_date)
                )
            """)
            conn.commit()
        finally:
            conn.close()

    async def initialize(self) -> None:
        """Asynchronously initializes the state database."""
        await asyncio.to_thread(self._init_db)

    def _get_rpd_sync(self, model_name: str, date_str: str) -> int:
        """Fetches daily usage from database. Runs in worker thread."""
        conn = sqlite3.connect(self.db_path, timeout=20.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            cursor = conn.cursor()
            cursor.execute(
                "SELECT request_count FROM daily_usage WHERE model_name = ? AND usage_date = ?",
                (model_name, date_str)
            )
            row = cursor.fetchone()
            return row[0] if row else 0
        finally:
            conn.close()

    async def get_rpd(self, model_name: str, date_str: str) -> int:
        """Gets the daily request count for a model (thread-safe, non-blocking)."""
        async with self.db_lock:
            return await asyncio.to_thread(self._get_rpd_sync, model_name, date_str)

    def _increment_rpd_sync(self, model_name: str, date_str: str) -> int:
        """Increments and returns the daily request count. Runs in worker thread."""
        conn = sqlite3.connect(self.db_path, timeout=20.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO daily_usage (model_name, usage_date, request_count) "
                "VALUES (?, ?, 1) "
                "ON CONFLICT(model_name, usage_date) DO UPDATE SET request_count = request_count + 1",
                (model_name, date_str)
            )
            conn.commit()
            
            cursor.execute(
                "SELECT request_count FROM daily_usage WHERE model_name = ? AND usage_date = ?",
                (model_name, date_str)
            )
            row = cursor.fetchone()
            return row[0] if row else 1
        finally:
            conn.close()

    async def increment_rpd(self, model_name: str, date_str: str) -> int:
        """Increments the daily request count for a model (thread-safe, non-blocking)."""
        async with self.db_lock:
            return await asyncio.to_thread(self._increment_rpd_sync, model_name, date_str)

    def get_rpm(self, model_name: str) -> int:
        """Gets the number of requests made in the last 60 seconds (RAM check)."""
        now = time.time()
        if model_name not in self.rpm_logs:
            return 0
        
        log = self.rpm_logs[model_name]
        # Remove timestamps older than 60 seconds
        while log and log[0] < now - 60.0:
            log.popleft()
        
        return len(log)

    def record_request_ram(self, model_name: str) -> None:
        """Records a request timestamp in RAM for RPM and spacing tracking."""
        now = time.time()
        if model_name not in self.rpm_logs:
            self.rpm_logs[model_name] = deque()
        self.rpm_logs[model_name].append(now)
        self.last_request_time[model_name] = now

    def get_spacing_delay(self, model_name: str, spacing_required: float) -> float:
        """Calculates the time to wait to satisfy spacing requirements (RAM check)."""
        now = time.time()
        last_time = self.last_request_time.get(model_name, 0.0)
        elapsed = now - last_time
        if elapsed < spacing_required:
            return spacing_required - elapsed
        return 0.0


class GeminiOrchestrator:
    """
    Middleware that routes and optimizes calls to Gemini API models:
    - Enforces Free Tier RPD & RPM constraints.
    - Spaces out requests.
    - Handles automatic routing and fallback with exponential backoff.
    """
    # Free tier model specifications
    MODEL_SPECS = {
        "gemini-2.5-flash-lite": {"rpm": 10, "rpd": 20, "spacing": 6.0},
        "gemini-2.5-flash": {"rpm": 5, "rpd": 20, "spacing": 12.0},
        "gemini-3.5-flash": {"rpm": 5, "rpd": 20, "spacing": 12.0}
    }
    
    # Priority order for models
    PRIORITY_LIST = ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-3.5-flash"]

    def __init__(self, api_key: str, db_path: str = "gemini_orchestrator_state.db"):
        self.api_key = api_key
        genai.configure(api_key=self.api_key)
        self.state_manager = GeminiStateManager(db_path=db_path)
        self._initialized = False

    async def _ensure_initialized(self) -> None:
        if not self._initialized:
            await self.state_manager.initialize()
            self._initialized = True

    def get_current_google_date(self) -> str:
        """Gets current date in America/Los_Angeles timezone, falling back to UTC-8 if zoneinfo fails."""
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
        except Exception as e:
            logger.warning(f"ZoneInfo America/Los_Angeles failed, using fallback UTC-8: {e}")
            # Fallback to standard UTC-8 (timedelta -8 hours from UTC)
            return (datetime.now(timezone.utc) - timedelta(hours=8)).strftime("%Y-%m-%d")

    async def get_available_model(self, exclude_models: Optional[List[str]] = None) -> Optional[str]:
        """
        Returns the first model in the priority list that is currently within its daily and minute quotas.
        If all models are exhausted, returns None.
        """
        await self._ensure_initialized()
        date_str = self.get_current_google_date()
        exclude = exclude_models or []
        
        for model in self.PRIORITY_LIST:
            if model in exclude:
                continue
            specs = self.MODEL_SPECS[model]
            
            # Check Daily limit (RPD)
            rpd = await self.state_manager.get_rpd(model, date_str)
            if rpd >= specs["rpd"]:
                logger.debug(f"Model {model} hit RPD limit ({rpd}/{specs['rpd']})")
                continue
                
            # Check Minute limit (RPM)
            rpm = self.state_manager.get_rpm(model)
            if rpm >= specs["rpm"]:
                logger.debug(f"Model {model} hit RPM limit ({rpm}/{specs['rpm']})")
                continue
                
            # Model is available!
            return model
            
        return None

    async def generate_content(
        self,
        prompt: str,
        generation_config: Optional[Dict[str, Any]] = None,
        system_instruction: Optional[str] = None
    ) -> str:
        """
        Asynchronously generates content, routing to the optimal model and handling retries/fallbacks.
        Raises AllModelsExhaustedError if no models can handle the request.
        """
        await self._ensure_initialized()
        date_str = self.get_current_google_date()
        
        # We retry and fallback across models
        attempted_models = []
        base_backoff = 1.0
        
        while len(attempted_models) < len(self.PRIORITY_LIST):
            # 1. Identify the best available model
            model_name = await self.get_available_model(exclude_models=attempted_models)
            
            if not model_name:
                # If we've already attempted some models, we can raise, otherwise all are blocked by rate limits
                raise AllModelsExhaustedError(
                    "All Gemini models have exhausted their quotas (RPD or RPM) for the current window."
                )
                
            if model_name in attempted_models:
                # Avoid infinite loops or retrying same exhausted model in this single call chain
                break
                
            attempted_models.append(model_name)
            specs = self.MODEL_SPECS[model_name]
            
            # 2. Check spacing and sleep if needed
            spacing_delay = self.state_manager.get_spacing_delay(model_name, specs["spacing"])
            if spacing_delay > 0:
                logger.info(f"Spacing request: sleeping {spacing_delay:.2f}s for model {model_name}")
                await asyncio.sleep(spacing_delay)
            
            # 3. Record the request execution in RAM
            self.state_manager.record_request_ram(model_name)
            
            # 4. Instantiate GenerativeModel
            model = genai.GenerativeModel(
                model_name=model_name,
                system_instruction=system_instruction
            )
            
            # 5. Call API
            logger.info(f"Routing request to {model_name} (Daily Date: {date_str})")
            try:
                # We use generate_content_async since we want a fully async execution.
                # However, in older SDKs or edge cases, we wrap inside asyncio.to_thread just in case.
                if hasattr(model, "generate_content_async"):
                    response = await model.generate_content_async(
                        prompt,
                        generation_config=generation_config
                    )
                else:
                    response = await asyncio.to_thread(
                        model.generate_content,
                        prompt,
                        generation_config=generation_config
                    )
                
                # Check response validity
                if not response or not response.text:
                    raise ValueError("Empty response text from Gemini API.")
                
                # Succeeded! Now persist RPD increment to database
                await self.state_manager.increment_rpd(model_name, date_str)
                logger.info(f"Successful generation using {model_name}.")
                return response.text.strip()
                
            except exceptions.ResourceExhausted as e:
                # 429 Error or Quota limit reached dynamically
                logger.warning(f"ResourceExhausted (429) from {model_name}: {e}. Retrying fallback...")
                
                # Apply exponential backoff with jitter before trying next model
                sleep_time = base_backoff * (2 ** len(attempted_models)) + random.uniform(0.1, 1.0)
                logger.info(f"Exponential backoff: sleeping {sleep_time:.2f}s before fallback.")
                await asyncio.sleep(sleep_time)
                
            except Exception as e:
                # Other unexpected failures (e.g. 500, parsing exceptions)
                logger.error(f"Unexpected error calling model {model_name}: {e}. Falling back...")
                
                # We also backoff slightly for safety
                await asyncio.sleep(1.0)
                
        # If we exit the loop, all models have failed or exhausted
        raise AllModelsExhaustedError(
            "Failed to generate content: All available models were attempted and failed, or are fully exhausted."
        )

def compress_context(prompt: str, max_chars: int = 15000) -> str:
    """
    Utility function to compress the input prompt size to fit within strict token limits.
    Compresses prompt by reducing the items list if it's JSON, not by raw string truncation.
    """
    if len(prompt) <= max_chars:
        return prompt
    
    logger.info(f"Prompt length {len(prompt)} exceeds limit {max_chars}. Compressing by reducing items...")
    
    try:
        import json
        parts = prompt.split("\n\n", 1)
        if len(parts) == 2:
            header, json_str = parts
            data = json.loads(json_str)
            if "items_to_select_from" in data and isinstance(data["items_to_select_from"], list):
                items = data["items_to_select_from"]
                original_count = len(items)
                while len(items) > 1:
                    items.pop()
                    new_json = json.dumps(data, ensure_ascii=False, indent=2)
                    new_prompt = f"{header}\n\n{new_json}"
                    if len(new_prompt) <= max_chars:
                        logger.info(f"Compressed prompt from {original_count} to {len(items)} items. Length: {len(new_prompt)}")
                        return new_prompt
                
                # If even 1 item is too long, try removing recently_published_titles if present
                if "recently_published_titles" in data:
                    del data["recently_published_titles"]
                    new_json = json.dumps(data, ensure_ascii=False, indent=2)
                    new_prompt = f"{header}\n\n{new_json}"
                    if len(new_prompt) <= max_chars:
                        logger.info("Removed recently_published_titles to fit context.")
                        return new_prompt
    except Exception as e:
        logger.warning(f"Failed to compress context via JSON parsing: {e}. Falling back to raw truncation.")
    
    # Ultimate fallback if parsing fails or structure doesn't match
    half_limit = max_chars // 2
    return prompt[:half_limit] + "\n\n... [CONTEXT TRUNCATED FOR TOKEN OPTIMIZATION] ...\n\n" + prompt[-half_limit:]
