import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "bot.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=8080,
        access_log=False,
        timeout_graceful_shutdown=30,
    )
