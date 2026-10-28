def main() -> None:
    import uvicorn

    from travel_agent.config import get_settings

    settings = get_settings()
    uvicorn.run("travel_agent.api.app:app", host=settings.host, port=settings.port, reload=True)
