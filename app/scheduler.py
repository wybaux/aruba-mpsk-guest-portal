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
        from app.db import db
        from app.notification_service import notification_service

        logger.info(f"Background cleanup task started (checking every {self.interval_seconds}s)")
        while self._running:
            try:
                # 1. Clean up fully expired passes
                revoked = self.aruba_client.clean_expired_passes()
                if revoked:
                    logger.info(f"Cleaned up {len(revoked)} expired Wi-Fi passes: {', '.join(revoked)}")

                # 2. Check and alert passes nearing expiration (<= 15 minutes)
                passes_to_warn = db.get_passes_needing_expiry_alert(threshold_minutes=15)
                for p in passes_to_warn:
                    if p.get("guest_email"):
                        notification_service.send_expiry_alert(
                            guest_name=p["guest_name"],
                            recipient_email=p["guest_email"],
                            guest_id=p["id"],
                            expires_at_str=p["expires_at"],
                            remaining_minutes=15
                        )
                    db.mark_expiry_alert_sent(p["id"])
                    logger.info(f"Sent impending expiration alert for pass {p['id']} ({p['guest_name']})")
            except Exception as e:
                logger.error(f"Error during scheduled background task: {e}")
            
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
