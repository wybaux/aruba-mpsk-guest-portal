import asyncio
import logging
from app.config import settings
from app.aruba_client import BaseArubaClient

logger = logging.getLogger("wifi_guest.scheduler")

class CleanupScheduler:
    def __init__(self, aruba_client: BaseArubaClient, interval_seconds: int = 60):
        self.aruba_client = aruba_client
        self.interval_seconds = interval_seconds
        self._task = None
        self._running = False

    async def _run_loop(self):
        logger.info(f"Background cleanup task started (checking every {self.interval_seconds}s)")
        while self._running:
            try:
                revoked = self.aruba_client.clean_expired_passes()
                if revoked:
                    logger.info(f"Cleaned up {len(revoked)} expired Wi-Fi passes: {', '.join(revoked)}")
            except Exception as e:
                logger.error(f"Error during scheduled expired passes cleanup: {e}")
            
            await asyncio.sleep(self.interval_seconds)

    def start(self):
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._run_loop())

    def stop(self):
        if self._running:
            self._running = False
            if self._task:
                self._task.cancel()
                logger.info("Background cleanup task stopped")
