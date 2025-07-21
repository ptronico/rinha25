import os
import time
import asyncio
import redis.asyncio as redis
from contextlib import asynccontextmanager
from fastapi import FastAPI
from pydantic import BaseModel

redis_client: redis.Redis = None
task_queue = asyncio.Queue()


async def task_worker():
    while True:
        item = await task_queue.get()
        print(f"new message: {item}")
        # await asyncio.sleep(2)
        task_queue.task_done()


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("ONSTART")
    global redis_client
    redis_client = redis.Redis(
        host=os.environ["REDIS_HOST"],
        port=int(os.environ["REDIS_PORT"])
    )
    await redis_client.ping()
    await redis_client.set("request_count", 0)
    asyncio.create_task(task_worker())

    yield
    print("ONSHUTDOWN")
    await redis_client.aclose()


app = FastAPI(lifespan=lifespan)


@app.get("/ping")
async def read_root():
    return {"ping": "pong"}


@app.get("/info")
async def read_root():
    item = await task_queue.put(str(time.time()))
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
    print(payment.model_dump_json())
    await redis_client.incr("request_count")
    return {"instance": os.environ.get("INSTANCE_ID", None)}


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
            "totalRequests": 0,
            "totalAmount": 0,
        },
        "fallback" : {
            "totalRequests": 0,
            "totalAmount": 0,
        }
    }
