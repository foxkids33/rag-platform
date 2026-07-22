import asyncio
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rag-worker")


async def main() -> None:
    logger.info("RAG worker skeleton started; ingestion queue will be added in the next milestone")
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
