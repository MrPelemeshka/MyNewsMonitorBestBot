#!/usr/bin/env python3
"""
Запуск бота с улучшенной обработкой ошибок
"""
import asyncio
import sys
import logging
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

def main():
    print("=" * 60)
    print("🤖 ЗАПУСК УЛУЧШЕННОГО ТЕЛЕГРАМ БОТА")
    print(f"📅 {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
    print("=" * 60)
    print("✨ Новые возможности:")
    print("• 🎯 Умный поиск с весовыми ключевыми словами")
    print("• 📊 Детальная аналитика и рекомендации")
    print("• ⚡ Фоновая обработка очереди отправки")
    print("• 🏷️ Автоматическая категоризация новостей")
    print("=" * 60)
    print("Бот запущен. Нажмите Ctrl+C для остановки.")
    print("Логи сохраняются в bot.log")
    print("=" * 60)
    
    try:
        from bot import main as bot_main
        asyncio.run(bot_main())
    except KeyboardInterrupt:
        print("\n\n👋 Бот остановлен пользователем")
        sys.exit(0)
    except Exception as e:
        print(f"\n\n❌ Критическая ошибка: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()