# performance_monitor.py 1.0.0
import time
import logging
from typing import Dict, Any, List
from dataclasses import dataclass
import asyncio

log = logging.getLogger("performance_monitor")

@dataclass
class RequestMetrics:
    start_time: float
    end_time: float
    success: bool
    tokens_used: int
    status_code: int = 0

class PerformanceMonitor:
    """Мониторинг производительности LLM запросов"""
    
    def __init__(self, window_size: int = 100):
        self.window_size = window_size
        self.requests: List[RequestMetrics] = []
        self.lock = asyncio.Lock()
        
        # Статистика
        self.total_requests = 0
        self.failed_requests = 0
        self.total_tokens = 0
        
    async def record_request(self, start_time: float, end_time: float, success: bool, tokens_used: int, status_code: int = 0):
        """Запись метрик запроса"""
        async with self.lock:
            metrics = RequestMetrics(start_time, end_time, success, tokens_used, status_code)
            self.requests.append(metrics)
            self.total_requests += 1
            self.total_tokens += tokens_used
            
            if not success:
                self.failed_requests += 1
            
            # Поддерживаем размер окна
            if len(self.requests) > self.window_size:
                self.requests.pop(0)
    
    def get_current_stats(self) -> Dict[str, Any]:
        """Текущая статистика производительности"""
        if not self.requests:
            return {}
        
        now = time.time()
        window_start = now - 300  # 5 минут
        
        recent_requests = [r for r in self.requests if r.end_time >= window_start]
        
        if not recent_requests:
            return {}
        
        successful_requests = [r for r in recent_requests if r.success]
        failed_requests = [r for r in recent_requests if not r.success]
        
        total_time = sum(r.end_time - r.start_time for r in recent_requests)
        avg_response_time = total_time / len(recent_requests) if recent_requests else 0
        
        tokens_per_minute = sum(r.tokens_used for r in recent_requests) / 5  # за 5 минут
        
        return {
            "requests_5min": len(recent_requests),
            "success_rate": len(successful_requests) / len(recent_requests) if recent_requests else 0,
            "avg_response_time_seconds": avg_response_time,
            "tokens_per_minute": tokens_per_minute,
            "current_concurrent_estimate": min(len(recent_requests) // 5, 50)  # примерная оценка
        }
    
    def get_recommendations(self) -> List[str]:
        """Рекомендации по оптимизации на основе метрик"""
        stats = self.get_current_stats()
        recommendations = []
        
        if stats.get("avg_response_time_seconds", 0) > 10:
            recommendations.append("Высокое время ответа LLM - рассмотрите увеличение таймаутов")
        
        if stats.get("success_rate", 1) < 0.95:
            recommendations.append("Низкий процент успешных запросов - проверьте стабильность соединения")
        
        if stats.get("tokens_per_minute", 0) > 50000:
            recommendations.append("Высокое использование токенов - возможны ограничения по квотам")
        
        return recommendations