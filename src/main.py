import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Type

import httpx
import redis.asyncio as redis
from fastapi import FastAPI, Request, Response
from pydantic import BaseModel

from .connection_manager import HttpClient

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


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
            logging.warning(f"DEFAULT payment processor is {message}")
        self._default_failing = is_failing

    @property
    def fallback_failing(self):
        return self._fallback_failing

    @fallback_failing.setter
    def fallback_failing(self, is_failing: bool):
        if is_failing != self._fallback_failing:
            message = "FAILING" if is_failing else "WORKING"
            logging.warning(f"FALLBACK payment processor is {message}")
        self._fallback_failing = is_failing


redis_client: redis.Redis = None
default_http_client: Type[HttpClient] = HttpClient(base_url="http://payment-processor-default:8080", name="DEFAULT")
fallback_http_client: Type[HttpClient] = HttpClient(base_url="http://payment-processor-fallback:8080", name="FALLBACK")
payment_processor_health: Type[PaymentProcessorHealth] = PaymentProcessorHealth()


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
        await redis_client.zadd(cls._default_db_name, {amount: cls._datetime_str_to_number(datetime_str)})

    @classmethod
    async def default_summary(cls, start_datetime_str: str, end_datetime_str: str):
        txs: list = await redis_client.zrangebyscore(
            cls._default_db_name,
            cls._datetime_str_to_number(start_datetime_str),
            cls._datetime_str_to_number(end_datetime_str),
        )
        n = len(txs)
        return (n, n * 19.9)

    @classmethod
    async def fallback_add(cls, datetime_str: str, amount: str):
        await redis_client.zadd(cls._fallback_db_name, {amount: cls._datetime_str_to_number(datetime_str)})

    @classmethod
    async def fallback_summary(cls, start_datetime_str: str, end_datetime_str: str):
        txs: list = await redis_client.zrangebyscore(
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
        await redis_client.rpush(cls.name, message)

    @classmethod
    async def pop(cls):
        _, message = await redis_client.blpop(cls.name)
        return message.decode()


async def check_service_health(query_default: bool = True, interval_seconds: int = 5):
    while True:
        try:
            if query_default:
                health: dict = await default_http_client.get(endpoint="/payments/service-health")
            else:
                health: dict = await fallback_http_client.get(endpoint="/payments/service-health")

            if query_default:
                payment_processor_health.default_failing = health["failing"]
            else:
                payment_processor_health.fallback_failing = health["failing"]
        except Exception:
            logging.warning(f"query_default: {query_default} | Error")
        await asyncio.sleep(interval_seconds)


async def task_worker(idx: int):
    logging.warning(f"Starting queue {idx}")
    while True:
        message = await Queue.pop()
        data = json.loads(message)
        if not payment_processor_health.default_failing:
            try:
                await default_http_client.post(endpoint="/payments", payload=data)
                await TransactionsDB.default_add(datetime_str=data["requestedAt"], amount=message)
                continue
            except httpx.HTTPStatusError:
                logging.error("[httpx.HTTPStatusError] DEFAULT Error 500 -- lost message")
                payment_processor_health.default_failing = True
            except Exception as e:
                logging.error(f"[Exception] {e}")
                payment_processor_health.default_failing = True
                raise

        if not payment_processor_health.fallback_failing:
            try:
                await fallback_http_client.post(endpoint="/payments", payload=data)
                await TransactionsDB.fallback_add(datetime_str=data["requestedAt"], amount=message)
                continue
            except httpx.HTTPStatusError:
                logging.error("[httpx.HTTPStatusError] FALLBACK Error 500 -- lost message")
                payment_processor_health.fallback_failing = True
            except Exception as e:
                logging.error(f"[Exception] {e}")
                payment_processor_health.fallback_failing = True
                raise

        # Se a mensagem não foi processada com sucesso, adiciona ela novamente ao final da fila
        await Queue.push(message)

        # Se ambos estão fora do ar, espera um pouco até tentar novamente
        await asyncio.sleep(3)
        # logging.warning("both payment processors are failing -- sleeping for while")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client
    redis_client = redis.Redis(host=os.environ["REDIS_HOST"], port=int(os.environ["REDIS_PORT"]))
    await redis_client.ping()
    await redis_client.flushall()

    asyncio.create_task(check_service_health(query_default=True))
    asyncio.create_task(check_service_health(query_default=False))

    for idx in range(2000):
        asyncio.create_task(task_worker(idx))
    logging.warning("ONSTART")

    yield
    await redis_client.aclose()
    await default_http_client.disconnect()
    await fallback_http_client.disconnect()
    logging.warning("ONSHUTDOWN")


app = FastAPI(lifespan=lifespan)


class Payment(BaseModel):
    amount: float
    correlationId: str
    requestedAt: str | None = None


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
    payment.requestedAt = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    await Queue.push(payment.model_dump_json())
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
    logging.warning(f"summary params {params} {tf-t0}")
    return {
        "default": {
            "totalAmount": default_amount,
            "totalRequests": default_requests,
        },
        "fallback": {
            "totalAmount": fallback_amount,
            "totalRequests": fallback_requests,
        },
    }


@app.get("/ping")
async def ping_controller():
    return {
        "instance": os.environ.get("INSTANCE_ID", None),
    }


@app.post("/flushall")
async def flushall_controller():
    await redis_client.flushall()
    return {
        "flushall": True,
    }
