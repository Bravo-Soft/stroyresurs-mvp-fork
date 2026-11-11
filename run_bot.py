# run_bot.py 1.0.0
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# Добавляем пути к модулям
sys.path.insert(0, str(project_root / "graph_reports"))
sys.path.insert(0, str(project_root / "bot_report"))

# Запускаем бота через main_bot.py
if __name__ == "__main__":
    main_bot_path = project_root / "bot_report" / "main_bot.py"
    
    if main_bot_path.exists():
        from dotenv import load_dotenv
        from bot_logging import configure_bot_logging
        load_dotenv(project_root / '.env')
        configure_bot_logging()
        try:
            from bot_report.main_bot import main
            import asyncio
            asyncio.run(main())
        except Exception:
            import logging
            logging.getLogger('main_bot').exception('Ошибка запуска или работы бота')
            raise
    else:
        print(f"Файл {main_bot_path} не найден!")
        sys.exit(1)