"""Пул потоков для проверки паролей (REVIEW.md: AC-4).

scrypt — ~0,1 с CPU и 32 МиБ на попытку. В общем пуле `asyncio.to_thread` (его же берут
разбор источников и DNS) флуд входа занял бы все потоки. Свой пул ограничен
`ADMIN_KDF_THREADS`: лишние попытки ждут в его очереди, остальное приложение не страдает.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor

from app.modules.access.domain.passwords import verify_password


class KdfPool:
    def __init__(self, threads: int) -> None:
        self._executor = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="kdf")

    async def verify(self, password: str, encoded: str) -> bool:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, verify_password, password, encoded)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
