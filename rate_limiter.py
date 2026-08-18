# rate_limiter.py 1.0.0
import asyncio
import time
import logging
from collections import deque

log = logging.getLogger("rate_limiter")

class RateLimiter:
    def __init__(self, requests_per_10_seconds: int = 60):
        self.requests_per_10_seconds = requests_per_10_seconds
        self.window = 10  # секунды
        self.timestamps = deque()
        self.lock = asyncio.Lock()
        self.total_requests = 0
        self.failed_requests = 0

    async def acquire(self):
        async with self.lock:
            now = time.time()
            
            while self.timestamps and self.timestamps[0] <= now - self.window:
                self.timestamps.popleft()

            if len(self.timestamps) < self.requests_per_10_seconds:
                self.timestamps.append(now)
                self.total_requests += 1
                return True
            
            oldest = self.timestamps[0]
            wait_time = oldest + self.window - now
            if wait_time > 0:
                log.warning(f"Rate limit reached, waiting {wait_time:.2f}s "
                          f"({len(self.timestamps)}/{self.requests_per_10_seconds} requests in window)")
                await asyncio.sleep(wait_time)
               
                now = time.time()
                
                while self.timestamps and self.timestamps[0] <= now - self.window:
                    self.timestamps.popleft()

            self.timestamps.append(now)
            self.total_requests += 1
            return True

    def record_failure(self):
        self.failed_requests += 1

    def get_success_rate(self):
        if self.total_requests == 0:
            return 1.0
        return (self.total_requests - self.failed_requests) / self.total_requests