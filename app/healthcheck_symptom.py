import asyncio

from app.messengers import symptom_client


async def check() -> None:
    labels = await symptom_client.route_symptom("chest pain and shortness of breath")
    assert labels, "NLP symptom router returned no specialists for a clear-cut query"


if __name__ == "__main__":
    asyncio.run(check())
