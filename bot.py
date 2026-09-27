"""Executable entry point; dependencies and routers are composed by app.py."""
import asyncio


async def main() -> None:
    from app import main as run_application
    await run_application()


if __name__ == "__main__":
    asyncio.run(main())
