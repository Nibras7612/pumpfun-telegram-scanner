if __name__ == "__main__":
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError("Telegram secrets missing")
    asyncio.run(main())
