import asyncio, threading
from unittest.mock import patch
from forven.health_monitor import HealthMonitor, ComponentStatus, State


def test_blocked_health_collector_does_not_block_event_loop() -> None:
    async def run() -> None:
        entered = threading.Event()
        release = threading.Event()
        loop_thread = threading.get_ident()
        seen = []
        def blocked() -> ComponentStatus:
            seen.append(threading.get_ident())
            entered.set()
            release.wait(timeout=2)
            return ComponentStatus(name='scheduler', state=State.GREEN)
        monitor = HealthMonitor(poll_interval=100)
        monitor._running = True
        with patch('forven.health_monitor.check_scheduler', blocked):
            task = asyncio.create_task(monitor._poll_loop())
            try:
                for _ in range(100):
                    if entered.is_set():
                        break
                    await asyncio.sleep(.005)
                assert entered.is_set()
                assert seen == [seen[0]] and seen[0] != loop_thread
                assert not release.is_set()
            finally:
                release.set()
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
    asyncio.run(run())
