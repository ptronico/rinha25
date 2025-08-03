import asyncio
import logging
import os
import random
import socket
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import orjson
import redis.asyncio as redis
from fastapi import BackgroundTasks, FastAPI, Request, Response
from pydantic import BaseModel

from .connection_manager import HttpClient

hostname = socket.gethostname()
logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.default_http_client = HttpClient(base_url="http://payment-processor-default:8080", name="DEFAULT")
    app.state.fallback_http_client = HttpClient(base_url="http://payment-processor-fallback:8080", name="FALLBACK")
    app.state.payment_processor_health = PaymentProcessorHealth()

    redis_host = os.environ["REDIS_HOST"]
    redis_port = int(os.environ["REDIS_PORT"])

    app.state.default_num_sem = 15
    app.state.fallback_num_sem = 3

    app.state.default_semaphore = asyncio.Semaphore(app.state.default_num_sem)
    app.state.fallback_semaphore = asyncio.Semaphore(app.state.fallback_num_sem)

    app.state.redis_async_pool = redis.ConnectionPool(host=redis_host, port=redis_port)
    app.state.redis = redis.Redis.from_pool(app.state.redis_async_pool)

    await app.state.redis.ping()
    await app.state.redis.flushall()

    # asyncio.create_task(check_service_health(query_default=True))
    # asyncio.create_task(check_service_health(query_default=False))
    asyncio.create_task(task_worker(1))

    logging.warning("ONSTART")

    yield
    # app.state.redis.close()
    # await app.state.redis.aclose()
    await app.state.default_http_client.disconnect()
    await app.state.fallback_http_client.disconnect()
    logging.warning("ONSHUTDOWN")


app = FastAPI(lifespan=lifespan)


class PaymentProcessorHealth:
    def __init__(self):
        self._default_failing = True
        self._fallback_failing = True

    @property
    def default_failing(self):
        return self._default_failing

    @default_failing.setter
    def default_failing(self, is_failing: bool):
        if is_failing != self._default_failing:
            message = "FAILING" if is_failing else "WORKING"
            num_sem = 0 if is_failing else app.state.default_num_sem
            logging.warning(f"DEFAULT payment processor is {message} | sem: {num_sem}")
            # app.state.default_semaphore = asyncio.Semaphore(num_sem)
        self._default_failing = is_failing

    @property
    def fallback_failing(self):
        return self._fallback_failing

    @fallback_failing.setter
    def fallback_failing(self, is_failing: bool):
        if is_failing != self._fallback_failing:
            message = "FAILING" if is_failing else "WORKING"
            num_sem = 0 if is_failing else app.state.fallback_num_sem
            logging.warning(f"FALLBACK payment processor is {message} | sem: {num_sem}")
            # app.state.fallback_semaphore = asyncio.Semaphore(num_sem)
        self._fallback_failing = is_failing


class TransactionsDB:
    _default_db_name = "dtxs"
    _fallback_db_name = "ftxs"

    @classmethod
    def _datetime_str_to_number(cls, datetime_str: str) -> int:
        if "." in datetime_str:
            dt = datetime.strptime(datetime_str, "%Y-%m-%dT%H:%M:%S.%fZ")
        else:
            dt = datetime.strptime(datetime_str, "%Y-%m-%dT%H:%M:%S")
        dt = dt.replace(tzinfo=timezone.utc)
        value = int(dt.timestamp() * 1000)
        return value

    @classmethod
    async def default_add(cls, datetime_str: str, amount: str):
        await app.state.redis.zadd(cls._default_db_name, {amount: cls._datetime_str_to_number(datetime_str)})

    @classmethod
    async def default_summary(cls, start_datetime_str: str, end_datetime_str: str):
        txs: list = await app.state.redis.zrangebyscore(
            cls._default_db_name,
            cls._datetime_str_to_number(start_datetime_str),
            cls._datetime_str_to_number(end_datetime_str),
        )
        n = len(txs)
        return (n, n * 19.9)

    @classmethod
    async def fallback_add(cls, datetime_str: str, amount: str):
        await app.state.redis.zadd(cls._fallback_db_name, {amount: cls._datetime_str_to_number(datetime_str)})

    @classmethod
    async def fallback_summary(cls, start_datetime_str: str, end_datetime_str: str):
        txs: list = await app.state.redis.zrangebyscore(
            cls._fallback_db_name,
            cls._datetime_str_to_number(start_datetime_str),
            cls._datetime_str_to_number(end_datetime_str),
        )
        n = len(txs)
        return (n, n * 19.9)


class Queue:
    name = "default"

    @classmethod
    async def push(cls, message):
        r: redis.Redis = app.state.redis
        await r.rpush(cls.name, message)

    @classmethod
    async def re_push(cls, message):
        r: redis.Redis = app.state.redis
        await r.lpush(cls.name, message)

    @classmethod
    async def pop(cls):
        r: redis.Redis = app.state.redis
        _, message = await r.blpop(cls.name)
        return message.decode()

    @classmethod
    async def pop_many(cls, count=None):
        r: redis.Redis = app.state.redis
        messages = await r.lpop(cls.name, count)
        if isinstance(messages, str):
            return [messages.decode()]
        if messages is not None:
            return [m.decode() for m in messages]
        return []


async def check_service_health(query_default: bool = True, interval_seconds: int = 5):
    while True:
        try:
            if query_default:
                health: dict = await app.state.default_http_client.get(endpoint="/payments/service-health")
            else:
                health: dict = await app.state.fallback_http_client.get(endpoint="/payments/service-health")

            if query_default:
                app.state.payment_processor_health.default_failing = health["failing"]
            else:
                app.state.payment_processor_health.fallback_failing = health["failing"]
        except Exception:
            logging.warning(f"query_default: {query_default} | Error")
        await asyncio.sleep(interval_seconds)


async def process_payment(data, message):
    async with app.state.default_semaphore:
        try:
            await app.state.default_http_client.post(endpoint="/payments", payload=data)
            await TransactionsDB.default_add(datetime_str=data["requestedAt"], amount=message)
            # app.state.default_semaphore = asyncio.Semaphore(app.state.default_num_sem)
        except httpx.HTTPStatusError:
            # app.state.default_semaphore = asyncio.Semaphore(1)
            # await process_payment_fallback(data, message)
            await Queue.re_push(message)
        except Exception:
            raise


async def process_payment_fallback(data, message):
    async with app.state.fallback_semaphore:
        try:
            await app.state.fallback_http_client.post(endpoint="/payments", payload=data)
            await TransactionsDB.fallback_add(datetime_str=data["requestedAt"], amount=message)
        except httpx.HTTPStatusError:
            await Queue.re_push(message)
        except Exception:
            raise


async def task_worker(idx: int):
    # logging.warning(f"Starting queue {idx}")
    while True:
        # messages = [await Queue.pop()]
        messages = await Queue.pop_many(count=20)
        for message in messages:
            data = orjson.loads(message)
            if random.random() > 0.5:
                asyncio.create_task(process_payment(data, message=message))
            else:
                asyncio.create_task(process_payment_fallback(data, message=message))


class Payment(BaseModel):
    amount: float
    correlationId: str
    requestedAt: str | None = None


async def add_to_queue(payment: Payment):
    if not payment.requestedAt:
        payment.requestedAt = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    await Queue.push(payment.model_dump_json())


@app.post("/payments")
async def create_payment(payment: Payment, background_tasks: BackgroundTasks):
    """
    Ref: https://github.com/zanfranceschi/rinha-de-backend-2025/blob/main/INSTRUCOES.md#payments
    POST /payments
    {
        "correlationId": "4a7901b8-7d26-4d9d-aa19-4dc1c7cf60b3",
        "amount": 19.90
    }
    """
    # logging.warning(f"[payment] {payment.model_dump_json()}")
    background_tasks.add_task(add_to_queue, payment)
    return Response(status_code=202)


@app.get("/payments-summary")
async def payments_summary(request: Request):
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
    params = dict(request.query_params)
    # messages = await Queue.pop_many(count=10)
    # logging.warning(messages)
    # await asyncio.sleep(0.75)
    t0 = time.monotonic()
    default_requests, default_amount = await TransactionsDB.default_summary(
        start_datetime_str=params["from"],
        end_datetime_str=params["to"],
    )
    fallback_requests, fallback_amount = await TransactionsDB.fallback_summary(
        start_datetime_str=params["from"],
        end_datetime_str=params["to"],
    )
    tf = time.monotonic()
    delta_milis = int(1000 * (tf - t0))
    logging.warning(f"summary params {params} {delta_milis}ms | {default_requests} {fallback_requests}")
    return {
        "default": {
            "totalAmount": default_amount,
            "totalRequests": default_requests,
        },
        "fallback": {
            "totalAmount": fallback_amount,
            "totalRequests": fallback_requests,
        },
        "total": {
            "totalAmount": default_amount + fallback_amount,
            "totalRequests": default_requests + fallback_requests,
        },
    }


@app.get("/ping")
async def ping_controller():
    return {
        "hostname": hostname,
    }


@app.post("/flushall")
async def flushall_controller():
    await app.state.redis.flushall()
    return {
        "flushall": True,
    }
