import os
import json
import time
import httpx
import asyncio
import logging
import redis.asyncio as redis
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI
from pydantic import BaseModel

utc_dt = datetime.now(timezone.utc)
redis_client: redis.Redis = None


class Cache:
    @classmethod
    def _gateway_failing_key(cls, query_default: bool) -> str:
        return f"gateway:failign:default" if query_default else f"gateway:failign:fallback"

    @classmethod
    def _gateway_total_amount_key(cls, query_default: bool) -> str:
        return f"gateway:total-amount:default" if query_default else f"gateway:total-amount:fallback"

    @classmethod
    def _gateway_total_requests_key(cls, query_default: bool) -> str:
        return f"gateway:total-requests:default" if query_default else f"gateway:total-requests:fallback"

    @classmethod
    async def get_gateway_failing(cls, query_default: bool) -> bool:
        return "true" == await redis_client.get(cls._gateway_failing_key(query_default))

    @classmethod
    async def set_gateway_failing(cls, query_default: bool, failing: bool) -> None:
        await redis_client.set(cls._gateway_failing_key(query_default), "true" if failing else "false")

    @classmethod
    async def get_total_amount(cls, query_default: bool) -> bool:
        return float(await redis_client.get(cls._gateway_total_amount_key(query_default)))

    @classmethod
    async def set_total_amount(cls, query_default: bool, amount: float) -> bool:
        await redis_client.set(cls._gateway_total_amount_key(query_default), str(amount))

    @classmethod
    async def incr_total_amount(cls, query_default: bool, amount: float) -> bool:
        curr_amount = await cls.get_total_amount(query_default)
        return float(await redis_client.set(cls._gateway_total_amount_key(query_default), str(curr_amount + amount)))

    @classmethod
    async def get_total_requests(cls, query_default: bool) -> bool:
        return int(await redis_client.get(cls._gateway_total_requests_key(query_default)))

    @classmethod
    async def set_total_requests(cls, query_default: bool, requests: int) -> bool:
        return int(await redis_client.set(cls._gateway_total_requests_key(query_default), str(requests)))

    @classmethod
    async def incr_total_requests(cls, query_default: bool, requests: int) -> bool:
        await redis_client.incr(cls._gateway_total_requests_key(query_default), requests)


class Queue:
    name = "default"

    @classmethod
    async def push(cls, message):
        await redis_client.rpush(cls.name, message)

    @classmethod
    async def pop(cls):
        _, message = await redis_client.blpop(cls.name)
        return message.decode()


class PaymentGateway:
    default = "http://payment-processor-default:8080"
    fallback = "http://payment-processor-fallback:8080"

    @classmethod
    async def service_health(cls, query_default: bool = True) -> dict:
        gateway = cls.default if query_default else cls.fallback
        async with httpx.AsyncClient() as client:
            url = f"{gateway}/payments/service-health"
            response: httpx.Response = await client.get(url)
            return response.json()

    @classmethod
    async def pay(cls, data: dict, query_default: bool = True) -> bool:
        """
        NOTE: Esse método abre uma nova conexão a cada pagamento.
        Um método mais eficiente utilizaria a mesma conexão para fazer diversos pagamentos.
        """
        gateway = cls.default if query_default else cls.fallback
        async with httpx.AsyncClient(timeout=3) as client:
            url = f"{gateway}/payments"
            response: httpx.Response = await client.post(url, json=data)
            return response.json()


async def check_service_health(query_default: bool = True, interval_seconds: int = 5):
    while True:
        try:
            health: dict = await PaymentGateway.service_health(query_default)
            await Cache.set_gateway_failing(query_default, health["failing"])
            # print(f"query_default: {query_default} | failing: {health['failing']} | minResponseTime: {health['minResponseTime']}")
        except Exception as exc:
            await Cache.set_gateway_failing(query_default, True)
            print(f"query_default: {query_default} | Error")
        await asyncio.sleep(interval_seconds)


async def task_worker(idx: int):
    while True:
        message = await Queue.pop()
        default_failing = await Cache.get_gateway_failing(query_default=True)
        fallback_failing = await Cache.get_gateway_failing(query_default=False)
        # print(f"default_failing: {default_failing} | fallback_failing: {fallback_failing}")
        now = utc_dt.strftime("%Y-%m-%dT%H:%M:%S")
        data = json.loads(message) | {"requestedAt": f"{now}.000Z"}
        # if default_failing and fallback_failing:
        #     print("BOTH FAILING")
        #     await asyncio.sleep(2)
        #     continue
        # if default_failing:
        #     print("DEFAULT FAILING")
        #     await asyncio.sleep(2)
        #     continue
        # if fallback_failing:
        #     print("FALLBACK FAILING")
        query_default = fallback_failing if default_failing else default_failing
        query_default = True
        try:
            response = await PaymentGateway.pay(data, query_default=query_default)
            await Cache.incr_total_amount(query_default=query_default, amount=data["amount"])
            await Cache.incr_total_requests(query_default=query_default, requests=1)
        except:
            await asyncio.sleep(5)
        # try:
        # except Exception as exc:
        #     print(f"Error: {exc}")
        # print(f"idx: {idx} | was_paid: {was_paid} | default_failing: {default_failing} | fallback_failing: {fallback_failing}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client
    redis_client = redis.Redis(
        host=os.environ["REDIS_HOST"],
        port=int(os.environ["REDIS_PORT"])
    )
    await redis_client.ping()
    if not await redis_client.exists("request_count"):
        await redis_client.set("request_count", 0)
    for query_default in [True, False]:
        # if not await redis_client.exists(Cache._gateway_total_amount_key(True)):
        await Cache.set_total_amount(query_default=query_default, amount=0)
        # if not await redis_client.exists(Cache._gateway_total_requests_key(True)):
        await Cache.set_total_requests(query_default=query_default, requests=0)
        # if not await redis_client.exists(Cache._gateway_failing_key(True)):
        await Cache.set_gateway_failing(query_default=query_default, failing=True)

    asyncio.create_task(check_service_health(query_default=True))
    asyncio.create_task(check_service_health(query_default=False))
    for idx in range(2):
        asyncio.create_task(task_worker(idx))
    print("ONSTART")

    yield
    await redis_client.aclose()
    print("ONSHUTDOWN")


app = FastAPI(lifespan=lifespan)


@app.get("/ping")
async def read_root():
    return {"ping": "pong"}


@app.get("/info")
async def read_root():
    message = {"time": time.time()}
    item = await Queue.push(json.dumps(message))
    return {
        "code": "1",
        "instance": os.environ.get("INSTANCE_ID", None),
        "request_count": int(await redis_client.get("request_count")),
    }


class Payment(BaseModel):
    amount: float
    correlationId: str


@app.post("/payments")
async def create_payment(payment: Payment):
    """
    Ref: https://github.com/zanfranceschi/rinha-de-backend-2025/blob/main/INSTRUCOES.md#payments
    POST /payments
    {
        "correlationId": "4a7901b8-7d26-4d9d-aa19-4dc1c7cf60b3",
        "amount": 19.90
    }
    """
    await Queue.push(payment.model_dump_json())
    await redis_client.incr("request_count")
    return {
        "queued": True,
        "instance": os.environ.get("INSTANCE_ID", None)
    }


@app.get("/payments-summary")
async def payments_summary():
    """
    Ref: https://github.com/zanfranceschi/rinha-de-backend-2025/blob/main/INSTRUCOES.md#payments-summary
    GET /payments-summary?from=2020-07-10T12:34:56.000Z&to=2020-07-10T12:35:56.000Z
    HTTP 200 - Ok
    {
        "default" : {
            "totalRequests": 43236,
            "totalAmount": 415542345.98
        },
        "fallback" : {
            "totalRequests": 423545,
            "totalAmount": 329347.34
        }
    }
    """
    return {
        "default" : {
            "totalAmount": await Cache.get_total_amount(query_default=True),
            "totalRequests": await Cache.get_total_requests(query_default=True),
        },
        "fallback" : {
            "totalAmount": await Cache.get_total_amount(query_default=False),
            "totalRequests": await Cache.get_total_requests(query_default=False),
        }
    }
